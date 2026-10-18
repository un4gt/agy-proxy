"""Compare ClientHello structure while excluding random/session/key bytes."""
import collections
import hashlib
import json
import struct
from pathlib import Path

def hello(body):
    pos=34
    pos+=1+body[pos]
    n=int.from_bytes(body[pos:pos+2],'big'); pos+=2
    ciphers=[int.from_bytes(body[i:i+2],'big') for i in range(pos,pos+n,2)]; pos+=n
    pos+=1+body[pos]
    total=int.from_bytes(body[pos:pos+2],'big'); pos+=2
    end=pos+total
    result={'legacy_version':int.from_bytes(body[:2],'big'),'cipher_suites':ciphers,'extensions':[]}
    while pos<end:
        kind,n=struct.unpack_from('>HH',body,pos); pos+=4
        data=body[pos:pos+n];pos+=n
        result['extensions'].append(kind)
        if kind==0:result['sni']=data[5:5+int.from_bytes(data[3:5],'big')].decode()
        elif kind in (10,13):result[str(kind)]=[int.from_bytes(data[i:i+2],'big') for i in range(2,len(data),2)]
        elif kind==11:result['point_formats']=list(data[1:])
        elif kind==43:result['versions']=[int.from_bytes(data[i:i+2],'big') for i in range(1,len(data),2)]
        elif kind==16:
            values=[];p=2
            while p<len(data):values.append(data[p+1:p+1+data[p]].decode());p+=1+data[p]
            result['alpn']=values
        elif kind==51:
            shares=[];p=2
            while p<len(data):
                group,size=struct.unpack_from('>HH',data,p);shares.append([group,size]);p+=4+size
            result['key_share_shapes']=shares
    return result

def inspect(path):
    raw=Path(path).read_bytes()
    endian='<' if raw[:4] in (b'\xd4\xc3\xb2\xa1',b'\x4d\x3c\xb2\xa1') else '>'
    assert struct.unpack_from(endian+'I',raw,20)[0]==1
    pos=24; streams=collections.defaultdict(list)
    while pos<len(raw):
        sec,usec,n,orig=struct.unpack_from(endian+'IIII',raw,pos);pos+=16
        pkt=raw[pos:pos+n];pos+=n
        if pkt[12:14]!=b'\x08\x00':continue
        ip=pkt[14:];h=(ip[0]&15)*4
        if ip[9]!=6:continue
        tcp=ip[h:int.from_bytes(ip[2:4],'big')];th=(tcp[12]>>4)*4
        data=tcp[th:]
        if not data:continue
        key=(ip[12:16],ip[16:20],tcp[:2],tcp[2:4])
        streams[key].append((int.from_bytes(tcp[4:8],'big'),data))
    results=[]
    for fragments in streams.values():
        fragments.sort();assembled=bytearray();end=None
        for seq,data in fragments:
            if end is None:end=seq
            if seq>end:break
            skip=max(0,end-seq);assembled.extend(data[skip:]);end=max(end,seq+len(data))
        offset=0
        while True:
            at=assembled.find(b'\x16\x03',offset)
            if at<0:break
            offset=at+2
            if at+9>=len(assembled) or assembled[at+5]!=1:continue
            record_size=int.from_bytes(assembled[at+3:at+5],'big')
            size=int.from_bytes(assembled[at+6:at+9],'big')
            if size+4>record_size or at+9+size>len(assembled):continue
            try:results.append(hello(bytes(assembled[at+9:at+9+size])))
            except (ValueError,IndexError,struct.error,UnicodeError):continue
    return results

result={mode:inspect('/tmp/'+mode+'.pcap') for mode in ('baseline','hooked')}
sets={mode:{json.dumps(item,sort_keys=True) for item in items} for mode,items in result.items()}
result['same_unique_client_hello_structures']=sets['baseline']==sets['hooked']
Path('/evidence/tls-fingerprints.json').write_text(json.dumps(result,indent=2))
print(json.dumps({'counts':{mode:len(result[mode]) for mode in sets},'same_unique_structures':result['same_unique_client_hello_structures'],
                  'servers':{mode:sorted({item.get('sni','') for item in result[mode]}) for mode in sets}},indent=2))
