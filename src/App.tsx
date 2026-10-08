import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import {
  Activity,
  ArrowDownLeft,
  ArrowUpRight,
  ArrowRight,
  ArrowLeft,
  Box,
  CircleHelp,
  Copy,
  ExternalLink,
  KeyRound,
  Layers,
  LayoutDashboard,
  Loader2,
  LogOut,
  Plus,
  RefreshCw,
  ShieldCheck,
  Terminal,
  Trash2,
  Pencil,
  Zap,
} from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  Field,
  FieldDescription,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import { Toaster } from "@/components/ui/sonner";
import { cn } from "@/lib/utils";
import { Accounts } from "@/components/accounts";
import {
  api,
  copy,
  date,
  message,
  number,
  zero,
  type Dashboard,
  type Key,
  type Model,
} from "@/lib/api";

function Brand() {
  return (
    <a
      href="/"
      className="flex items-center gap-2.5 font-semibold tracking-tight"
    >
      <span className="flex size-8 items-center justify-center rounded-lg bg-primary text-primary-foreground">
        <Layers className="size-4" />
      </span>
      <span>agy-proxy</span>
    </a>
  );
}
function ErrorNotice({ error }: { error: string }) {
  return error ? (
    <Alert variant="destructive">
      <CircleHelp />
      <AlertTitle>暂时无法完成操作</AlertTitle>
      <AlertDescription>{error}</AlertDescription>
    </Alert>
  ) : null;
}
function Blank({ title, description }: { title: string; description: string }) {
  return (
    <Empty>
      <EmptyHeader>
        <EmptyMedia variant="icon">
          <Box />
        </EmptyMedia>
        <EmptyTitle>{title}</EmptyTitle>
        <EmptyDescription>{description}</EmptyDescription>
      </EmptyHeader>
    </Empty>
  );
}
async function copyText(text: string) {
  try {
    await copy(text);
    toast.success("已复制");
  } catch {
    toast.error("复制失败，请手动选择并复制");
  }
}
function SignIn() {
  const [key, setKey] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [auth, setAuth] = useState<boolean | null>(null);
  useEffect(() => {
    api<{ auth_required: boolean }>("/health")
      .then((d) => setAuth(d.auth_required))
      .catch((e) => setError(message(e)));
  }, []);
  async function submit(e: FormEvent) {
    e.preventDefault();
    setPending(true);
    setError("");
    try {
      await api("/signin", { method: "POST", body: JSON.stringify({ key }) });
      location.assign("/");
    } catch {
      setError("密码不正确或连接失败，请检查后重试。");
      setPending(false);
    }
  }
  return (
    <div className="grid min-h-svh lg:grid-cols-2">
      <aside className="dark hidden flex-col justify-between bg-background p-12 text-foreground lg:flex xl:p-16">
        <Brand />
        <div className="mx-auto flex w-full max-w-md flex-col gap-9 py-14">
          <Badge variant="outline">
            <Terminal data-icon="inline-start" /> ANTIGRAVITY WORKSPACE
          </Badge>
          <div className="flex flex-col gap-5">
            <h1 className="text-4xl leading-tight font-semibold tracking-tight xl:text-5xl">
              一个入口，
              <br />
              连接你的 AI 工作流。
            </h1>
            <p className="max-w-sm text-base leading-7 text-muted-foreground">
              统一访问模型、管理 API
              密钥、查看每一次调用。让连接更简单，让用量更清晰。
            </p>
          </div>
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Layers className="size-4" /> 统一模型接口
              </CardTitle>
              <CardDescription>连接你习惯使用的工具</CardDescription>
            </CardHeader>
            <CardContent className="flex flex-wrap gap-2">
              <Badge variant="secondary">OpenAI 兼容</Badge>
              <Badge variant="secondary">Gemini 兼容</Badge>
              <Badge variant="secondary">流式响应</Badge>
            </CardContent>
            <CardFooter className="gap-2 text-sm text-muted-foreground">
              <ShieldCheck className="size-4" /> 独立密钥 · 用量追踪 · 集中管理
            </CardFooter>
          </Card>
        </div>
        <p className="text-xs text-muted-foreground">
          Antigravity CLI 的轻量 API 控制台
        </p>
      </aside>
      <main className="flex min-h-svh flex-col px-6 py-8 sm:px-12">
        <div className="flex items-center justify-between">
          <div className="lg:invisible">
            <Brand />
          </div>
          <Badge variant="secondary">控制台登录</Badge>
        </div>
        <div className="mx-auto flex w-full max-w-sm flex-1 flex-col justify-center gap-8 py-16">
          <div className="flex flex-col gap-3">
            <span className="flex size-11 items-center justify-center rounded-xl border bg-muted/50">
              <KeyRound className="size-5" />
            </span>
            <h2 className="mt-2 text-3xl font-semibold tracking-tight">
              欢迎回来
            </h2>
            <p className="text-sm leading-6 text-muted-foreground">
              登录 agy-proxy，管理你的模型与 API 服务。
            </p>
          </div>
          <ErrorNotice error={error} />
          {auth === null ? (
            <Skeleton className="h-32 w-full" />
          ) : auth ? (
            <form onSubmit={submit}>
              <FieldGroup>
                <Field data-invalid={!!error}>
                  <FieldLabel htmlFor="password">控制台密码</FieldLabel>
                  <Input
                    id="password"
                    type="password"
                    placeholder="输入控制台密码"
                    autoComplete="current-password"
                    required
                    value={key}
                    onChange={(e) => setKey(e.target.value)}
                    aria-invalid={!!error}
                  />
                  <FieldDescription>
                    使用服务管理员设置的登录密码。
                  </FieldDescription>
                </Field>
                <Button
                  size="lg"
                  type="submit"
                  disabled={pending || !key.trim()}
                >
                  {pending ? (
                    <Loader2
                      data-icon="inline-start"
                      className="animate-spin"
                    />
                  ) : null}
                  {pending ? "正在登录…" : "登录控制台"}
                  <ArrowRight data-icon="inline-end" />
                </Button>
              </FieldGroup>
            </form>
          ) : (
            <>
              <Alert>
                <ShieldCheck />
                <AlertTitle>未设置鉴权</AlertTitle>
                <AlertDescription>
                  当前服务无需密码，可以直接进入控制台。
                </AlertDescription>
              </Alert>
              <Button asChild size="lg">
                <a href="/">
                  进入控制台
                  <ArrowRight data-icon="inline-end" />
                </a>
              </Button>
            </>
          )}
          <Separator />
          <p className="flex items-center justify-center gap-2 text-xs text-muted-foreground">
            <ShieldCheck className="size-3.5" /> 控制台密码与客户端 API
            密钥独立管理
          </p>
        </div>
        <p className="text-center text-xs text-muted-foreground">
          agy-proxy · 简单连接，专注创造
        </p>
      </main>
    </div>
  );
}
function Shell({ children, page }: { children: ReactNode; page: string }) {
  async function signout() {
    try {
      await api("/signout");
      location.assign("/signin");
    } catch (e) {
      toast.error(message(e));
    }
  }
  return (
    <div className="min-h-svh bg-muted/30">
      <header className="border-b bg-background">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-4 px-5 py-4 sm:px-8">
          <div className="flex items-center gap-6">
            <Brand />
            <Separator orientation="vertical" className="hidden h-5 sm:block" />
            <span className="hidden text-sm text-muted-foreground sm:block">
              工作空间
            </span>
          </div>
          <nav className="flex items-center gap-1">
            <Button
              variant={page === "概览" ? "secondary" : "ghost"}
              size="sm"
              asChild
            >
              <a href="/">
                <LayoutDashboard data-icon="inline-start" />
                概览
              </a>
            </Button>
            <Button
              variant={page === "账号详情" ? "secondary" : "ghost"}
              size="sm"
              asChild
            >
              <a href="/login">账号详情</a>
            </Button>
            <Button
              variant={page === "CLI 更新" ? "secondary" : "ghost"}
              size="sm"
              asChild
            >
              <a href="/update">CLI 更新</a>
            </Button>
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label="退出登录"
              onClick={signout}
            >
              <LogOut />
            </Button>
          </nav>
        </div>
      </header>
      <main className="mx-auto max-w-7xl px-5 py-8 sm:px-8 sm:py-10">
        {children}
      </main>
      <footer className="mx-auto flex max-w-7xl items-center justify-between px-5 pb-8 text-xs text-muted-foreground sm:px-8">
        <span>agy-proxy</span>
        <span>Powered by Antigravity CLI</span>
      </footer>
    </div>
  );
}
function KeyManager({ keys, refresh }: { keys: Key[]; refresh: () => void }) {
  const [dialog, setDialog] = useState(false);
  const [editing, setEditing] = useState<Key | null>(null);
  const [deleting, setDeleting] = useState<Key | null>(null);
  const [name, setName] = useState("");
  const [secret, setSecret] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  function open(k?: Key) {
    setEditing(k || null);
    setName(k?.name || "");
    setError("");
    setDialog(true);
  }
  async function save(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const result = await api<{ key?: string }>(
        editing ? `/api/keys/${encodeURIComponent(editing.id)}` : "/api/keys",
        { method: editing ? "PATCH" : "POST", body: JSON.stringify({ name }) },
      );
      setDialog(false);
      if (result.key) setSecret(result.key);
      toast.success(editing ? "备注已更新" : "API 密钥已创建");
      refresh();
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  }
  async function remove() {
    if (!deleting) return;
    setBusy(true);
    try {
      await api(`/api/keys/${encodeURIComponent(deleting.id)}`, {
        method: "DELETE",
      });
      setDeleting(null);
      refresh();
      toast.success("密钥已删除");
    } catch (e) {
      toast.error(message(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <Card>
        <CardHeader className="gap-4 sm:flex sm:flex-row sm:items-center sm:justify-between">
          <div className="flex flex-col gap-2">
            <CardTitle>API 密钥</CardTitle>
            <CardDescription>
              为每个客户端分配独立密钥，随时管理访问权限。
            </CardDescription>
          </div>
          <Button onClick={() => open()}>
            <Plus data-icon="inline-start" />
            创建密钥
          </Button>
        </CardHeader>
        <CardContent>
          {keys.length ? (
            <Table>
              <TableHeader>
                <TableRow>
                  {[
                    "备注",
                    "密钥",
                    "请求",
                    "Tokens",
                    "最近使用",
                    "创建时间",
                    "操作",
                  ].map((h) => (
                    <TableHead key={h}>{h}</TableHead>
                  ))}
                </TableRow>
              </TableHeader>
              <TableBody>
                {keys.map((k) => (
                  <TableRow key={k.id}>
                    <TableCell className="font-medium">{k.name}</TableCell>
                    <TableCell className="font-mono text-xs">
                      {k.masked}
                    </TableCell>
                    <TableCell>{number(k.requests)}</TableCell>
                    <TableCell>{number(k.usage.total_tokens)}</TableCell>
                    <TableCell>{date(k.last_used_at)}</TableCell>
                    <TableCell>{date(k.created_at)}</TableCell>
                    <TableCell>
                      <div className="flex gap-1">
                        <Button
                          variant="ghost"
                          size="icon-sm"
                          aria-label={`重命名 ${k.name}`}
                          onClick={() => open(k)}
                        >
                          <Pencil />
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon-sm"
                          aria-label={`删除 ${k.name}`}
                          onClick={() => setDeleting(k)}
                        >
                          <Trash2 />
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ) : (
            <Blank
              title="创建你的第一把密钥"
              description="将密钥填入 Cherry Studio、OpenWebUI 或 SDK，即可开始调用。"
            />
          )}
        </CardContent>
      </Card>
      <Dialog
        open={dialog}
        onOpenChange={(v) => {
          if (!busy) setDialog(v);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {editing ? "修改密钥备注" : "创建 API 密钥"}
            </DialogTitle>
            <DialogDescription>
              使用容易识别的名字，例如客户端名称。
            </DialogDescription>
          </DialogHeader>
          <form onSubmit={save} className="flex flex-col gap-6">
            <ErrorNotice error={error} />
            <FieldGroup>
              <Field>
                <FieldLabel htmlFor="key-name">备注名称</FieldLabel>
                <Input
                  id="key-name"
                  placeholder="例如 Cherry Studio"
                  value={name}
                  maxLength={60}
                  onChange={(e) => setName(e.target.value)}
                />
              </Field>
            </FieldGroup>
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                disabled={busy}
                onClick={() => setDialog(false)}
              >
                取消
              </Button>
              <Button disabled={busy}>
                {busy && <Loader2 className="animate-spin" />}
                {editing ? "保存" : "创建密钥"}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
      <Dialog
        open={!!secret}
        onOpenChange={(v) => {
          if (!v) setSecret("");
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>密钥已就绪</DialogTitle>
            <DialogDescription>
              完整密钥仅显示这一次，请复制并妥善保存。
            </DialogDescription>
          </DialogHeader>
          <code className="break-all rounded-md bg-muted p-4 text-sm select-all">
            {secret}
          </code>
          <p className="text-sm text-muted-foreground">
            客户端 Base URL：<code>{location.origin}/v1</code>
          </p>
          <DialogFooter>
            <Button variant="outline" onClick={() => setSecret("")}>
              已保存
            </Button>
            <Button onClick={() => copyText(secret)}>
              <Copy data-icon="inline-start" />
              复制完整密钥
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <AlertDialog
        open={!!deleting}
        onOpenChange={(v) => {
          if (!v && !busy) setDeleting(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>删除这把密钥？</AlertDialogTitle>
            <AlertDialogDescription>
              「{deleting?.name}
              」将立即失效，使用它的客户端无法继续调用。此操作无法撤销。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={busy}>取消</AlertDialogCancel>
            <Button variant="destructive" disabled={busy} onClick={remove}>
              {busy ? "删除中…" : "删除密钥"}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
function Overview() {
  const [data, setData] = useState<Dashboard | null>(null);
  const [models, setModels] = useState<Model[]>([]);
  const [error, setError] = useState("");
  const [modelError, setModelError] = useState("");
  const [query, setQuery] = useState("");
  const [version, setVersion] = useState("");
  const [tick, setTick] = useState(0);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    async function load() {
      setBusy(true);
      try {
        const d = await api<Dashboard>("/dashboard");
        let merged = d.models;
        try {
          const list = await api<{ models: { id: string; label: string }[] }>(
            "/api/models",
          );
          const stats = new Map(d.models.map((m) => [m.model, m]));
          merged = list.models.map((m) => ({
            model: m.id,
            label: m.label,
            requests: stats.get(m.id)?.requests || 0,
            usage: stats.get(m.id)?.usage || zero,
          }));
          merged.push(
            ...d.models.filter(
              (m) => !list.models.some((x) => x.id === m.model),
            ),
          );
          if (alive) setModelError("");
        } catch (e) {
          if (alive) setModelError(message(e));
        }
        if (alive) {
          setData(d);
          setModels(
            merged.sort(
              (a, b) =>
                b.usage.total_tokens - a.usage.total_tokens ||
                a.model.localeCompare(b.model),
            ),
          );
          setError("");
        }
      } catch (e) {
        if (alive) setError(message(e));
      } finally {
        if (alive) {
          setBusy(false);
          timer = setTimeout(load, 5000);
        }
      }
    }
    load();
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [tick]);
  useEffect(() => {
    api<{ version: string }>("/update/status")
      .then((s) => setVersion(s.version))
      .catch(() => {});
  }, []);
  const refresh = () => setTick((t) => t + 1);
  const u = data?.totals.usage || zero;
  const filtered = models.filter((m) =>
    `${m.model} ${m.label}`.toLowerCase().includes(query.toLowerCase()),
  );
  const metrics = [
    {
      label: "总 Token 用量",
      value: u.total_tokens,
      detail: "由 agy usage 统计",
      icon: Zap,
    },
    {
      label: "请求总数",
      value: data?.totals.requests || 0,
      detail: `${data?.model_count || 0} 个模型产生用量`,
      icon: Activity,
    },
    {
      label: "输入 Tokens",
      value: u.input_tokens,
      detail: `缓存命中 ${number(u.cache_read_tokens)}`,
      icon: ArrowDownLeft,
    },
    {
      label: "输出 Tokens",
      value: u.output_tokens,
      detail: `思考 Tokens ${number(u.thinking_tokens)}`,
      icon: ArrowUpRight,
    },
  ];
  return (
    <Shell page="概览">
      <div className="mb-8 flex flex-wrap items-center justify-between gap-4">
        <div className="flex flex-col gap-2">
          <div className="flex items-center gap-3">
            <h1 className="text-2xl font-semibold tracking-tight">
              工作空间概览
            </h1>
            {version && <Badge variant="outline">agy v{version}</Badge>}
          </div>
          <p className="text-sm text-muted-foreground">
            查看模型用量，管理你的 API 连接。
          </p>
        </div>
        <Button variant="outline" onClick={refresh} disabled={busy}>
          <RefreshCw
            data-icon="inline-start"
            className={cn(busy && "animate-spin")}
          />
          刷新数据
        </Button>
      </div>
      <ErrorNotice error={error} />
      <div className="my-6 grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {metrics.map((m) => (
          <Card key={m.label}>
            <CardHeader className="flex flex-row items-center justify-between">
              <CardDescription>{m.label}</CardDescription>
              <m.icon className="size-4 text-muted-foreground" />
            </CardHeader>
            <CardContent>
              {data ? (
                <div className="text-3xl font-semibold tracking-tight tabular-nums">
                  {number(m.value)}
                </div>
              ) : (
                <Skeleton className="h-9 w-28" />
              )}
              <p className="mt-2 text-xs text-muted-foreground">{m.detail}</p>
            </CardContent>
          </Card>
        ))}
      </div>
      <Tabs defaultValue="models" className="gap-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <TabsList>
            <TabsTrigger value="models">可用模型</TabsTrigger>
            <TabsTrigger value="keys">API 密钥</TabsTrigger>
            <TabsTrigger value="recent">最近请求</TabsTrigger>
          </TabsList>
          <span className="text-xs text-muted-foreground">
            {error ? "连接中断" : busy ? "同步中…" : "每 5 秒自动同步"}
          </span>
        </div>
        <TabsContent value="models">
          <Card>
            <CardHeader className="gap-4 sm:flex sm:flex-row sm:items-center sm:justify-between">
              <div className="flex flex-col gap-2">
                <CardTitle>
                  模型目录{" "}
                  <span className="ml-2 text-sm font-normal text-muted-foreground">
                    {models.length}
                  </span>
                </CardTitle>
                <CardDescription>
                  所有模型的调用次数与 Token 消耗。
                </CardDescription>
              </div>
              <div className="w-full sm:w-64">
                <Input
                  aria-label="搜索模型"
                  placeholder="搜索模型名称…"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                />
              </div>
            </CardHeader>
            <CardContent>
              <ErrorNotice error={modelError} />
              {!data ? (
                <Skeleton className="h-72 w-full" />
              ) : !filtered.length ? (
                <Blank
                  title={query ? "没有匹配的模型" : "暂无可用模型"}
                  description={
                    query
                      ? "尝试其他关键词。"
                      : "连接 Antigravity 账号后刷新模型列表。"
                  }
                />
              ) : (
                <Table>
                  <TableHeader>
                    <TableRow>
                      {[
                        "模型",
                        "请求",
                        "输入",
                        "输出",
                        "思考",
                        "缓存",
                        "合计 Tokens",
                      ].map((h) => (
                        <TableHead key={h}>{h}</TableHead>
                      ))}
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {filtered.map((m) => (
                      <TableRow key={m.model}>
                        <TableCell>
                          <div className="flex items-center gap-3 py-2">
                            <div className="flex size-9 shrink-0 items-center justify-center rounded-lg border bg-muted/40">
                              <Box className="size-4 text-muted-foreground" />
                            </div>
                            <div>
                              <div className="font-mono text-xs font-medium">
                                {m.model}
                              </div>
                              <div className="mt-1 text-xs text-muted-foreground">
                                {m.label || "Antigravity 模型"}
                              </div>
                            </div>
                          </div>
                        </TableCell>
                        {[
                          m.requests,
                          m.usage.input_tokens,
                          m.usage.output_tokens,
                          m.usage.thinking_tokens,
                          m.usage.cache_read_tokens,
                          m.usage.total_tokens,
                        ].map((n, i) => (
                          <TableCell key={i} className="tabular-nums">
                            {number(n)}
                          </TableCell>
                        ))}
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </CardContent>
          </Card>
        </TabsContent>
        <TabsContent value="keys">
          {data ? (
            <KeyManager keys={data.keys} refresh={refresh} />
          ) : (
            <Skeleton className="h-64 w-full" />
          )}
        </TabsContent>
        <TabsContent value="recent">
          <Card>
            <CardHeader>
              <CardTitle>最近请求</CardTitle>
              <CardDescription>
                最近 {data?.recent.length || 0} 条 / 共{" "}
                {data?.recent_total || 0} 条调用记录。
              </CardDescription>
            </CardHeader>
            <CardContent>
              {!data?.recent.length ? (
                <Blank
                  title="还没有请求记录"
                  description="通过 API 发起调用后，在这里查看状态、耗时与用量。"
                />
              ) : (
                <Table>
                  <TableHeader>
                    <TableRow>
                      {[
                        "时间",
                        "模型",
                        "密钥",
                        "状态",
                        "耗时",
                        "输入",
                        "输出",
                        "Tokens",
                      ].map((h) => (
                        <TableHead key={h}>{h}</TableHead>
                      ))}
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {data.recent.map((r, i) => (
                      <TableRow key={`${r.ts}-${i}`}>
                        <TableCell>{date(r.ts)}</TableCell>
                        <TableCell className="font-mono text-xs">
                          {r.model}
                        </TableCell>
                        <TableCell>{r.key_name}</TableCell>
                        <TableCell>
                          <Badge
                            variant={
                              r.status === "SUCCESS"
                                ? "secondary"
                                : "destructive"
                            }
                          >
                            {r.status === "SUCCESS" ? "成功" : r.status}
                          </Badge>
                        </TableCell>
                        <TableCell>
                          {r.duration_ms
                            ? (r.duration_ms / 1000).toFixed(1) + "s"
                            : "—"}
                        </TableCell>
                        <TableCell>{number(r.usage.input_tokens)}</TableCell>
                        <TableCell>{number(r.usage.output_tokens)}</TableCell>
                        <TableCell>{number(r.usage.total_tokens)}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
    </Shell>
  );
}

type BridgeState = {
  state: string;
  message?: string;
  url?: string;
  tail?: string;
  output?: string;
  version?: string;
  interactive?: boolean;
  session_id?: string;
  screen?: { text: string; selected: boolean; bold: boolean }[][];
};
const cliKeys = [
  ["up", "↑ 上"],
  ["down", "↓ 下"],
  ["left", "← 左"],
  ["right", "→ 右"],
  ["enter", "回车确认"],
  ["space", "空格选择"],
  ["tab", "Tab 下一项"],
  ["shift_tab", "Shift+Tab 上一项"],
  ["escape", "Esc 返回"],
  ["yes", "Y"],
  ["no", "N"],
] as const;
function LoginTerminal({
  state,
  onChange,
}: {
  state: BridgeState;
  onChange: (state: BridgeState) => void;
}) {
  const [pending, setPending] = useState(false);
  const sending = useRef(false);
  async function send(key: string) {
    if (!state.interactive || sending.current) return;
    sending.current = true;
    setPending(true);
    try {
      const next = await api<BridgeState>("/login/input", {
        method: "POST",
        body: JSON.stringify({ key, session_id: state.session_id }),
      });
      onChange(next);
    } catch (e) {
      toast.error(message(e));
    } finally {
      sending.current = false;
      setPending(false);
    }
  }
  return (
    <Card>
      <CardHeader>
        <CardTitle>CLI 交互终端</CardTitle>
        <CardDescription>
          查看当前提示，用下方按键选择登录方式或授权选项。回车会确认当前选项，请先核对内容。
        </CardDescription>
      </CardHeader>
      <CardContent className="flex min-w-0 flex-col gap-4">
        <div
          role="region"
          aria-label="CLI 当前画面"
          tabIndex={0}
          className="max-h-96 min-h-32 overflow-auto rounded-md border bg-muted/40 p-4 outline-offset-2"
          onKeyDown={(e) => {
            if (
              e.target !== e.currentTarget ||
              e.ctrlKey ||
              e.metaKey ||
              e.altKey
            )
              return;
            const keys: Record<string, string> = {
              ArrowUp: "up",
              ArrowDown: "down",
              ArrowLeft: "left",
              ArrowRight: "right",
              Enter: "enter",
              " ": "space",
              Escape: "escape",
              y: "yes",
              Y: "yes",
              n: "no",
              N: "no",
            };
            // Keep Tab available to leave this region; toolbar buttons send Tab to the CLI.
            const key = keys[e.key];
            if (key) {
              e.preventDefault();
              if (!e.repeat) void send(key);
            }
          }}
        >
          <pre className="w-max min-w-full font-mono text-xs leading-5">
            {state.screen?.length
              ? state.screen.map((line, i) => (
                  <div key={i}>
                    {line.map((run, j) => (
                      <span
                        key={j}
                        className={cn(
                          run.selected && "bg-primary text-primary-foreground",
                          run.bold && "font-bold",
                        )}
                      >
                        {run.text}
                      </span>
                    ))}
                  </div>
                ))
              : state.tail || "正在等待 CLI 输出…"}
          </pre>
        </div>
        <div
          className="flex flex-wrap gap-2"
          role="group"
          aria-label="CLI 按键"
        >
          {cliKeys.map(([key, label]) => (
            <Button
              key={key}
              variant={key === "enter" ? "default" : "outline"}
              size="sm"
              disabled={!state.interactive || pending}
              onClick={() => send(key)}
            >
              {label}
            </Button>
          ))}
        </div>
        <p className="text-xs text-muted-foreground">
          也可以点击画面后使用键盘方向键、回车、空格和 Esc。Tab 可移出画面；向
          CLI 发送 Tab 请用上方按钮。
        </p>
      </CardContent>
    </Card>
  );
}
function Bridge({
  update = false,
  embedded = false,
}: {
  update?: boolean;
  embedded?: boolean;
}) {
  const base = update ? "/update" : "/login";
  const title = update ? "CLI 更新" : "账号授权";
  const [state, setState] = useState<BridgeState | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [code, setCode] = useState("");
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const d = await api<BridgeState>(base + "/status");
        if (alive) {
          setState(d);
          setError("");
        }
      } catch (e) {
        if (alive) setError(message(e));
      } finally {
        if (alive) timer = setTimeout(poll, 1500);
      }
    }
    poll();
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [base, tick]);
  async function action(path: string, body?: object) {
    setBusy(true);
    setError("");
    try {
      const d = await api<BridgeState>(base + path, {
        method: "POST",
        body: JSON.stringify({ ...body, session_id: state?.session_id }),
      });
      setState(d);
      if (path === "/code") setCode("");
      setTick((t) => t + 1);
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  }
  const running =
    busy || ["running", "starting", "exchanging"].includes(state?.state || "");
  const label: Record<string, string> = {
    idle: "尚未开始",
    starting: "正在启动",
    interacting: "等待手动操作",
    authenticated: "凭据已保存，待完成",
    waiting_code: "等待授权",
    exchanging: "正在验证授权码",
    running: "正在更新",
    done: update ? "更新完成" : "账号已连接",
    error: "操作失败",
  };
  const content = (
    <div className="mx-auto flex max-w-2xl flex-col gap-6">
      {!embedded && (
        <>
          <Button variant="ghost" size="sm" asChild className="self-start">
            <a href="/">
              <ArrowLeft data-icon="inline-start" />
              返回概览
            </a>
          </Button>
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
            <p className="mt-2 text-sm text-muted-foreground">
              {update
                ? "检查并更新 Antigravity CLI，无需重新部署服务。"
                : "连接 Google 账号，让你的应用开始使用 Antigravity 模型。"}
            </p>
          </div>
        </>
      )}
      <ErrorNotice error={error} />
      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <CardTitle>
              {update ? "Antigravity CLI" : "Antigravity 账号"}
            </CardTitle>
            <Badge
              variant={state?.state === "error" ? "destructive" : "secondary"}
            >
              {state ? label[state.state] || state.state : "正在检查…"}
            </Badge>
          </div>
          <CardDescription>
            {update
              ? `当前版本：${state?.version ? "v" + state.version : "读取中…"}`
              : "授权信息保存在当前服务，无需每次重新登录。"}
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-5">
          {state?.message && (
            <p className="text-sm text-muted-foreground">{state.message}</p>
          )}
          {!update && (
            <ol className="grid grid-cols-3 gap-3 text-xs text-muted-foreground">
              {["确认 CLI 提示", "登录 Google 账号", "提交授权码"].map(
                (s, i) => (
                  <li key={s} className="flex flex-col gap-2">
                    <span className="flex size-7 items-center justify-center rounded-full border bg-muted">
                      {i + 1}
                    </span>
                    {s}
                  </li>
                ),
              )}
            </ol>
          )}
          {state?.url && !update && (
            <div className="flex flex-wrap gap-2">
              <Button asChild>
                <a
                  href={/^https:\/\//.test(state.url) ? state.url : "#"}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  打开 Google 授权页
                  <ExternalLink data-icon="inline-end" />
                </a>
              </Button>
              <Button variant="outline" onClick={() => copyText(state.url!)}>
                <Copy data-icon="inline-start" />
                复制链接
              </Button>
            </div>
          )}
          {!update &&
            ["waiting_code", "exchanging"].includes(state?.state || "") && (
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  action("/code", { code });
                }}
              >
                <FieldGroup>
                  <Field>
                    <FieldLabel htmlFor="code">授权码</FieldLabel>
                    <Input
                      id="code"
                      value={code}
                      onChange={(e) => setCode(e.target.value)}
                      placeholder="粘贴 Google 授权后的 code"
                      required
                    />
                    <FieldDescription>
                      完成授权后，从跳转页面复制授权码并粘贴到这里。
                    </FieldDescription>
                  </Field>
                  <Button disabled={running || !code.trim()}>
                    提交授权码
                    <ArrowRight data-icon="inline-end" />
                  </Button>
                </FieldGroup>
              </form>
            )}
          {update && (
            <p className="text-sm leading-6 text-muted-foreground">
              更新会释放空闲会话；正在进行的请求会继续完成。
            </p>
          )}
        </CardContent>
        <CardFooter className="flex-wrap gap-2">
          {!update && state?.state === "authenticated" && (
            <Button disabled={busy} onClick={() => action("/finish")}>
              完成并关闭
            </Button>
          )}
          <Button
            disabled={!state || running || (!update && state.interactive)}
            onClick={() => action("/start")}
          >
            {running ? (
              <Loader2 data-icon="inline-start" className="animate-spin" />
            ) : (
              <RefreshCw data-icon="inline-start" />
            )}
            {running
              ? "处理中…"
              : update
                ? "检查并更新"
                : state?.state === "idle"
                  ? "开始连接"
                  : "重新连接"}
          </Button>
          {!update && state?.interactive && (
            <Button
              variant="outline"
              disabled={busy}
              onClick={() => action("/stop")}
            >
              取消本次登录
            </Button>
          )}
        </CardFooter>
      </Card>
      {!update &&
      state &&
      (state.interactive || state.screen?.length || state.tail) ? (
        <LoginTerminal state={state} onChange={setState} />
      ) : null}
      {update && (state?.output || state?.tail) && (
        <Card>
          <CardHeader>
            <CardTitle>运行日志</CardTitle>
          </CardHeader>
          <CardContent>
            <pre className="max-h-80 overflow-auto rounded-md bg-muted p-4 font-mono text-xs leading-6 whitespace-pre-wrap break-all">
              {state.output || state.tail}
            </pre>
          </CardContent>
        </Card>
      )}
    </div>
  );
  return embedded ? content : <Shell page={title}>{content}</Shell>;
}
export default function App() {
  const path = location.pathname;
  return (
    <>
      {path === "/signin" ? (
        <SignIn />
      ) : path === "/login" ? (
        <Shell page="账号详情">
          <Accounts login={<Bridge embedded />} />
        </Shell>
      ) : path === "/update" ? (
        <Bridge update />
      ) : (
        <Overview />
      )}
      <Toaster theme="light" richColors position="bottom-right" />
    </>
  );
}
