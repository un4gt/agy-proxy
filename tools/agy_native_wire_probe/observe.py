"""HTTP/1.1 and HTTP/2 observation PoC; persist only allowlisted metadata and test output."""
import gzip
import hashlib
import json
import itertools
import os
import time
from collections import defaultdict, deque
from hpack import Decoder

out = open(os.environ.get('AGY_WIRE_EVENTS', '/evidence/hook-events.jsonl'), 'w', buffering=1)
started = time.monotonic()
request_numbers = itertools.count(1)
cleaned_requests = set()

def audit_request(doc, request_id):
    """Explicit fixture-only audit; never persist transport headers or outer IDs."""
    directory = os.environ.get('AGY_WIRE_INPUT_AUDIT')
    if not directory or doc.get('requestType') != 'agent':
        return
    from pathlib import Path
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    request = doc.get('request', {})
    payload = {key: request[key] for key in ('systemInstruction', 'contents', 'tools') if key in request}
    path = root / f'{os.getpid()}-{request_id}.json'
    with path.open('x', encoding='utf-8') as target:
        os.chmod(path, 0o600)
        json.dump(payload, target, ensure_ascii=False, indent=2)
def emit(kind, **fields):
    out.write(json.dumps(dict(kind=kind, monotonic=time.monotonic(), elapsed=time.monotonic()-started, **fields))+'\n')

requests = defaultdict(deque)
class Stream:
    def __init__(self, conn, direction):
        self.conn, self.direction = conn, direction
        self.buffer = bytearray()
        self.reset()
    def reset(self):
        self.header = None
        self.body = bytearray()
        self.chunk = None
        self.remaining = None
        self.sse = bytearray()
        self.request_id = None
    def feed(self, value):
        self.buffer.extend(value)
        if len(self.buffer)+len(self.body) > 16*1024*1024:
            raise ValueError('capture exceeds bound')
        while self.buffer:
            if self.header is None:
                at = self.buffer.find(b'\r\n\r\n')
                if at < 0: return
                lines=bytes(self.buffer[:at]).split(b'\r\n')
                del self.buffer[:at+4]
                first=lines.pop(0).decode(errors='replace')
                self.header={k.decode().lower():v.strip().decode(errors='replace') for line in lines if b':' in line for k,v in [line.split(b':',1)]}
                if self.direction == 'send':
                    parts=first.split(' ')
                    self.path=parts[1] if len(parts)>1 else 'unknown'
                    self.path=self.path.split('?')[0]
                    self.request_id = str(next(request_numbers))
                    requests[self.conn].append((self.path,self.request_id))
                    emit('request',conn=self.conn,request_id=self.request_id,path=self.path,request_line=first.split('?')[0],
                         header_names=sorted(self.header),user_agent=self.header.get('user-agent'),
                         content_type=self.header.get('content-type'),authorization_present='authorization' in self.header)
                else:
                    self.path,self.request_id=requests[self.conn][0] if requests[self.conn] else ('unknown',None)
                    emit('response',conn=self.conn,request_id=self.request_id,path=self.path,status_line=first,content_type=self.header.get('content-type'))
                self.chunked='chunked' in self.header.get('transfer-encoding','')
                self.remaining=int(self.header.get('content-length','0')) if not self.chunked else None
                if self.remaining == 0:
                    self.done()
                    continue
            if self.chunked:
                if self.chunk is None:
                    at=self.buffer.find(b'\r\n')
                    if at<0:return
                    size=bytes(self.buffer[:at]).split(b';')[0]
                    try:self.chunk=int(size,16)
                    except ValueError:
                        emit('framing_error',direction=self.direction,path=self.path,request_id=self.request_id,
                             size_bytes=len(size),buffer_bytes=len(self.buffer),
                             looks_like_request=size.startswith((b'POST ',b'GET ')),empty=not size)
                        raise
                    del self.buffer[:at+2]
                if self.chunk==0:
                    if len(self.buffer)<2:return
                    if self.buffer[:2] != b'\r\n':raise ValueError('trailers unsupported')
                    del self.buffer[:2]
                    self.done()
                    continue
                if len(self.buffer)<self.chunk+2:return
                self.data(bytes(self.buffer[:self.chunk]))
                del self.buffer[:self.chunk+2]
                self.chunk=None
            else:
                n=min(len(self.buffer),self.remaining)
                self.data(bytes(self.buffer[:n]))
                del self.buffer[:n]
                self.remaining-=n
                if self.remaining==0:self.done()
    def data(self, data):
        self.body.extend(data)
        if self.direction=='recv' and 'text/event-stream' in self.header.get('content-type',''):
            self.sse.extend(data)
            while b'\n' in self.sse:
                line,_,rest=self.sse.partition(b'\n')
                self.sse=bytearray(rest)
                if line.startswith(b'data:'):
                    value=line[5:].strip()
                    if value==b'[DONE]':emit('sse_done',conn=self.conn,request_id=self.request_id,path=self.path)
                    else:
                        try:self.summarize(json.loads(value),'sse')
                        except ValueError:emit('sse_invalid',conn=self.conn,path=self.path)
    def summarize(self, doc, kind):
        if not isinstance(doc,dict):return
        response=doc.get('response',doc)
        if not isinstance(response,dict):return
        candidates=response.get('candidates',[])
        values=[]
        for candidate in candidates:
            content=candidate.get('content') or {}
            values.append({'finishReason':candidate.get('finishReason'),
                           'index':candidate.get('index',0),
                           'parts':[{'text':part.get('text'), 'thought':part.get('thought'),
                                     'functionCallName':(part.get('functionCall') or {}).get('name'),
                                     'functionCall':part.get('functionCall')}
                                    for part in content.get('parts',[])]})
        emit(kind,conn=self.conn,request_id=self.request_id,path=self.path,keys=sorted(doc),candidates=values,usage=response.get('usageMetadata'))
    def done(self):
        body=bytes(self.body)
        if self.direction=='send' and self.path.endswith(('generateContent','streamGenerateContent')):
            try:
                doc=json.loads(body)
                request=doc.get('request',{})
                if os.environ.get('AGY_WIRE_INPUT_CLEAN') == '1' and doc.get('requestType') == 'agent':
                    from input_clean import digest
                    if digest(doc) not in cleaned_requests:
                        raise ValueError('Agent input was not cleaned before transport')
                audit_request(doc, self.request_id)
                calling = (request.get('toolConfig') or {}).get('functionCallingConfig') or {}
                emit('generation_request',conn=self.conn,request_id=self.request_id,path=self.path,request_type=doc.get('requestType'),
                     function_calling_mode=calling.get('mode'),allowed_function_names=calling.get('allowedFunctionNames'),
                     model=doc.get('model'),outer_keys=sorted(doc),request_keys=sorted(request),
                     roles=[x.get('role') for x in request.get('contents',[])],
                     tool_names=[f.get('name') for t in request.get('tools',[]) for f in t.get('functionDeclarations',[])])
            except (ValueError,TypeError):emit('request_parse_error',conn=self.conn)
        elif self.direction=='recv':
            if 'text/event-stream' not in self.header.get('content-type','') and self.path.endswith(('generateContent','streamGenerateContent')):
                try:self.summarize(json.loads(gzip.decompress(body) if self.header.get('content-encoding')=='gzip' else body),'json_response')
                except (ValueError,OSError):emit('response_parse_error',conn=self.conn)
            if requests.get(self.conn):
                requests[self.conn].popleft()
                if not requests[self.conn]:requests.pop(self.conn)
        # Completion must follow parsed content, including non-SSE responses.
        emit('message_complete',conn=self.conn,request_id=self.request_id,direction=self.direction,path=self.path,bytes=len(body),sha256=hashlib.sha256(body).hexdigest())
        self.reset()

http2_connections=set()
http2_paths={}
class H2Stream:
    def __init__(self,conn,direction):
        self.conn,self.direction=conn,direction
        self.buffer=bytearray()
        self.decoder=Decoder(max_header_list_size=256*1024)
        self.blocks={}
        self.messages={}
        self.end_pending=set()
        self.preface=direction=='send'
    def feed(self,data):
        self.buffer.extend(data)
        if self.preface:
            if len(self.buffer)<24:return
            assert self.buffer[:24]==b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n'
            del self.buffer[:24]
            self.preface=False
            emit('http2_preface',conn=self.conn)
        while len(self.buffer)>=9:
            n=int.from_bytes(self.buffer[:3],'big')
            if len(self.buffer)<9+n:return
            kind,flags=self.buffer[3:5]
            stream=int.from_bytes(self.buffer[5:9],'big')&0x7fffffff
            data=bytes(self.buffer[9:9+n])
            del self.buffer[:9+n]
            if kind in (0,1) and flags&8:
                pad=data[0]
                data=data[1:len(data)-pad] if pad else data[1:]
            if kind==1 and flags&32:data=data[5:]
            if kind==1 and flags&1:self.end_pending.add(stream)
            if kind in (1,9):
                self.blocks.setdefault(stream,bytearray()).extend(data)
                if flags&4:
                    if stream in self.messages:
                        raise ValueError('HTTP/2 trailers unsupported')
                    headers=dict(self.decoder.decode(bytes(self.blocks.pop(stream))))
                    item=Stream(f'{self.conn}/{stream}',self.direction)
                    item.header=headers
                    key=(self.conn,stream)
                    if self.direction=='send':
                        item.path=headers.get(':path','unknown').split('?')[0]
                        item.request_id=str(next(request_numbers))
                        http2_paths[key]=(item.path,item.request_id)
                        emit('request',conn=self.conn,stream=stream,request_id=item.request_id,path=item.path,protocol='h2',
                             header_names=sorted(headers),user_agent=headers.get('user-agent'),
                             authority=headers.get(':authority'),content_type=headers.get('content-type'),
                             authorization_present='authorization' in headers)
                    else:
                        item.path,item.request_id=http2_paths.get(key,('unknown',None))
                        emit('response',conn=self.conn,stream=stream,request_id=item.request_id,path=item.path,status=headers.get(':status'),
                             protocol='h2',content_type=headers.get('content-type'))
                    self.messages[stream]=item
            elif kind==0:
                if stream in self.messages:self.messages[stream].data(data)
                else:emit('orphan_data',conn=self.conn,stream=stream)
            if (kind==0 and flags&1 or stream in self.end_pending and stream not in self.blocks) and stream in self.messages:
                self.messages.pop(stream).done()
                self.end_pending.discard(stream)
                if self.direction=='recv':http2_paths.pop((self.conn,stream),None)

streams={}

def capture_write(conn,data,written):
    if not 0 <= written <= len(data):
        raise ValueError('invalid TLS Write count')
    if written:
        capture(conn,'send',data[:written])
    if written != len(data):
        emit('short_write',conn=conn,requested=len(data),written=written)

def capture(conn,direction,data):
    key=(conn,direction)
    if direction=='send' and data.startswith(b'PRI * HTTP/2.0'):http2_connections.add(conn)
    if key not in streams:streams[key]=H2Stream(conn,direction) if conn in http2_connections else Stream(conn,direction)
    emit('tls_io',conn=conn,direction=direction,bytes=len(data))
    streams[key].feed(data)
