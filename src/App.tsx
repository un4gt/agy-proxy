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
  Database,
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
import { Switch } from "@/components/ui/switch";
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
import { Progress } from "@/components/ui/progress";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
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
  type Usage,
} from "@/lib/api";

let cachedAboutVersion: string | null = null;
let cachedAuthRequired: boolean | null = null;
let cachedUpdateVersion = "";
let cachedDashboard: Dashboard | null = null;
let cachedModels: ModelGroup[] | null = null;
let cachedOverviewTab = "models";
const cachedBridgeState = new Map<string, BridgeState>();

function navigate(href: string, e?: React.MouseEvent<HTMLAnchorElement>) {
  if (
    e &&
    (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0)
  ) {
    return;
  }
  e?.preventDefault();
  if (location.pathname !== href) {
    history.pushState(null, "", href);
    window.dispatchEvent(new PopStateEvent("popstate"));
  }
}

function useDelayedSkeleton(loading: boolean, delayMs = 180) {
  const [show, setShow] = useState(false);
  useEffect(() => {
    if (!loading) {
      setShow(false);
      return;
    }
    const timer = setTimeout(() => setShow(true), delayMs);
    return () => clearTimeout(timer);
  }, [loading, delayMs]);
  return loading && show;
}

function Brand() {
  const [version, setVersion] = useState<string | null>(cachedAboutVersion);
  useEffect(() => {
    const controller = new AbortController();
    api<{ version: string }>("/about", { signal: controller.signal })
      .then((data) => {
        if (!controller.signal.aborted) {
          cachedAboutVersion = data.version;
          setVersion(data.version);
        }
      })
      .catch(() => {});
    return () => controller.abort();
  }, []);
  return (
    <a
      href="/"
      onClick={(e) => {
        if (location.pathname !== "/signin") navigate("/", e);
      }}
      className="flex flex-wrap items-center gap-2.5 font-semibold tracking-tight"
    >
      <span className="flex size-8 items-center justify-center rounded-lg bg-primary text-primary-foreground shadow-xs">
        <Layers className="size-4" />
      </span>
      <span>agy-proxy</span>
      {version && (
        <Badge
          variant="outline"
          className="border-primary/20 bg-primary/5 text-primary"
          aria-label={`部署版本 v${version}`}
          title="当前部署的 agy-proxy 版本"
        >
          v{version}
        </Badge>
      )}
    </a>
  );
}
function ErrorNotice({ error }: { error: string }) {
  return error ? (
    <Alert variant="destructive" className="shrink-0 py-2.5">
      <CircleHelp />
      <AlertTitle>暂时无法完成操作</AlertTitle>
      <AlertDescription>{error}</AlertDescription>
    </Alert>
  ) : null;
}
function Blank({ title, description }: { title: string; description: string }) {
  return (
    <Empty className="my-auto min-h-[200px] border-border/70 bg-muted/20 py-6">
      <EmptyHeader>
        <EmptyMedia variant="icon" className="bg-primary/10 text-primary">
          <Box />
        </EmptyMedia>
        <EmptyTitle>{title}</EmptyTitle>
        <EmptyDescription>{description}</EmptyDescription>
      </EmptyHeader>
    </Empty>
  );
}
function LoadingStatus({ label }: { label: string }) {
  return (
    <p
      role="status"
      className="flex items-center gap-2 text-xs text-muted-foreground"
    >
      <Loader2 className="size-3.5 shrink-0 animate-spin" />
      {label}
    </p>
  );
}
function ModelsSkeleton() {
  return (
    <div aria-hidden="true">
      <Table>
        <TableHeader className="sticky top-0 z-10 bg-card/95 backdrop-blur-xs">
          <TableRow>
            {["模型", "包含变体", "启用"].map((h) => (
              <TableHead key={h}>{h}</TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {[0, 1, 2, 3, 4].map((row) => (
            <TableRow key={row}>
              <TableCell>
                <div className="flex items-center gap-2.5 py-0.5">
                  <Skeleton className="size-8 shrink-0 rounded-lg" />
                  <div className="flex flex-col gap-1.5">
                    <Skeleton className="h-3 w-32" />
                    <Skeleton className="h-3 w-24" />
                  </div>
                </div>
              </TableCell>
              <TableCell>
                <div className="flex flex-wrap gap-1.5">
                  <Skeleton className="h-5 w-12 rounded-full" />
                  <Skeleton className="h-5 w-16 rounded-full" />
                </div>
              </TableCell>
              <TableCell>
                <div className="flex items-center gap-2.5">
                  <Skeleton className="h-[1.15rem] w-8 rounded-full" />
                  <Skeleton className="h-3 w-10" />
                </div>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
function TableSkeleton({
  headers,
  rows = 4,
}: {
  headers: string[];
  rows?: number;
}) {
  return (
    <div aria-hidden="true">
      <Table>
        <TableHeader className="sticky top-0 z-10 bg-card/95 backdrop-blur-xs">
          <TableRow>
            {headers.map((h) => (
              <TableHead key={h}>{h}</TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {Array.from({ length: rows }, (_, row) => (
            <TableRow key={row}>
              {headers.map((h, col) => (
                <TableCell key={h}>
                  <Skeleton
                    className={cn("h-4", col === 0 ? "w-28" : "w-16")}
                  />
                </TableCell>
              ))}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
function cacheRate(usage: Usage): number | null {
  // CLI input/total exclude cache_read; the full prompt is uncached + cached.
  const prompt = (usage.input_tokens || 0) + (usage.cache_read_tokens || 0);
  return prompt > 0 ? ((usage.cache_read_tokens || 0) / prompt) * 100 : null;
}
function UsageStat({
  label,
  value,
  hint,
}: {
  label: string;
  value?: number | null;
  hint?: string;
}) {
  return (
    <div className="flex flex-col gap-0.5" title={hint}>
      <span className="text-[11px] text-muted-foreground">{label}</span>
      <span className="text-xs font-medium tabular-nums">
        {value === null || value === undefined ? "—" : number(value)}
      </span>
    </div>
  );
}
function RequestUsage({ usage }: { usage: Usage }) {
  const rate = cacheRate(usage);
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label="查看用量明细"
          className="flex flex-col items-start gap-0.5 rounded-sm text-left outline-none focus-visible:ring-2 focus-visible:ring-ring/50"
        >
          <span className="text-xs tabular-nums">
            入 {number(usage.input_tokens)} · 出 {number(usage.output_tokens)}
          </span>
          <span className="text-[11px] text-muted-foreground tabular-nums">
            缓存 {number(usage.cache_read_tokens)} ·{" "}
            {rate === null ? "—" : `${rate.toFixed(1)}%`}
          </span>
        </button>
      </PopoverTrigger>
      <PopoverContent align="end">
        <div className="flex flex-col gap-2.5">
          <div className="flex items-baseline justify-between gap-2">
            <span className="text-xs font-medium">用量明细</span>
            <span className="text-[11px] text-muted-foreground tabular-nums">
              {rate === null ? "缓存率 —" : `缓存率 ${rate.toFixed(1)}%`}
            </span>
          </div>
          <div className="grid grid-cols-2 gap-x-3 gap-y-2">
            <UsageStat label="未缓存输入" value={usage.input_tokens} />
            <UsageStat label="缓存读取" value={usage.cache_read_tokens} />
            <UsageStat
              label="缓存写入"
              value={null}
              hint="上游未上报缓存写入计数"
            />
            <UsageStat label="输出（含思考）" value={usage.output_tokens} />
            <UsageStat label="其中思考" value={usage.thinking_tokens} />
            <UsageStat
              label="总计（不含缓存）"
              value={usage.total_tokens}
              hint="CLI 上报的 total，不含缓存读取"
            />
          </div>
          {rate !== null && (
            <Progress value={rate} aria-label={`缓存命中率 ${rate.toFixed(1)}%`} />
          )}
          <p className="text-[11px] leading-4 text-muted-foreground">
            缓存率 = 缓存读取 ÷（未缓存输入 + 缓存读取）。上游未提供缓存写入计数。
          </p>
        </div>
      </PopoverContent>
    </Popover>
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
  const [auth, setAuth] = useState<boolean | null>(cachedAuthRequired);
  const showSkeleton = useDelayedSkeleton(auth === null);
  useEffect(() => {
    api<{ auth_required: boolean }>("/health")
      .then((d) => {
        cachedAuthRequired = d.auth_required;
        setAuth(d.auth_required);
      })
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
    <div className="grid min-h-svh bg-background lg:h-svh lg:grid-cols-2 lg:overflow-hidden">
      <aside className="dark relative hidden flex-col justify-between overflow-hidden bg-[radial-gradient(ellipse_at_top_left,oklch(0.28_0.09_264),oklch(0.15_0.028_262)_75%)] p-8 text-foreground lg:flex xl:p-12">
        <Brand />
        <div className="mx-auto flex w-full max-w-md flex-col gap-6 py-4">
          <Badge variant="outline" className="border-white/15 bg-white/5 text-white/90">
            <Terminal data-icon="inline-start" /> ANTIGRAVITY WORKSPACE
          </Badge>
          <div className="flex flex-col gap-3.5">
            <h1 className="text-3xl leading-tight font-semibold tracking-tight xl:text-4xl">
              一个入口，
              <br />
              连接你的 AI 工作流。
            </h1>
            <p className="max-w-sm text-sm leading-6 text-muted-foreground">
              统一访问模型、管理 API
              密钥、查看每一次调用。让连接更简单，让用量更清晰。
            </p>
          </div>
          <Card className="border-white/10 bg-white/5 shadow-lg backdrop-blur-sm">
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-sm">
                <Layers className="size-4 text-primary" /> 统一模型接口
              </CardTitle>
              <CardDescription>连接你习惯使用的工具</CardDescription>
            </CardHeader>
            <CardContent className="flex flex-wrap gap-2">
              <Badge variant="secondary">OpenAI 兼容</Badge>
              <Badge variant="secondary">Gemini 兼容</Badge>
              <Badge variant="secondary">流式响应</Badge>
            </CardContent>
            <CardFooter className="gap-2 text-xs text-muted-foreground">
              <ShieldCheck className="size-3.5 text-primary" /> 独立密钥 · 用量追踪 · 集中管理
            </CardFooter>
          </Card>
        </div>
        <p className="text-xs text-muted-foreground">
          Antigravity CLI 的轻量 API 控制台
        </p>
      </aside>
      <main className="flex min-h-svh flex-col justify-between px-6 py-6 sm:px-10 lg:h-svh lg:py-8">
        <div className="flex items-center justify-between">
          <div className="lg:invisible">
            <Brand />
          </div>
          <Badge variant="secondary">控制台登录</Badge>
        </div>
        <div className="mx-auto flex w-full max-w-sm flex-1 flex-col justify-center gap-6 py-4">
          <div className="flex flex-col gap-2">
            <span className="flex size-10 items-center justify-center rounded-xl border border-primary/20 bg-primary/10 text-primary">
              <KeyRound className="size-5" />
            </span>
            <h2 className="mt-1 text-2xl font-semibold tracking-tight sm:text-3xl">
              欢迎回来
            </h2>
            <p className="text-sm leading-6 text-muted-foreground">
              登录 agy-proxy，管理你的模型与 API 服务。
            </p>
          </div>
          <ErrorNotice error={error} />
          <div className="min-h-[148px] flex flex-col justify-center">
            {auth === null ? (
              showSkeleton ? <Skeleton className="h-32 w-full" /> : null
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
              <div className="flex flex-col gap-4">
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
              </div>
            )}
          </div>
          <Separator />
          <p className="flex items-center justify-center gap-2 text-xs text-muted-foreground">
            <ShieldCheck className="size-3.5 text-primary" /> 控制台密码与客户端 API
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
    <div className="flex min-h-svh flex-col bg-muted/35 lg:h-svh lg:overflow-hidden">
      <header className="shrink-0 border-b border-border/80 bg-card/90 backdrop-blur-md">
        <div className="mx-auto flex min-h-14 max-w-7xl flex-wrap items-center justify-between gap-3 px-4 py-2 sm:px-6">
          <div className="flex items-center gap-4">
            <Brand />
            <Separator orientation="vertical" className="hidden h-4 sm:block" />
            <span className="hidden text-xs font-medium text-muted-foreground sm:block">
              工作空间
            </span>
          </div>
          <nav className="flex items-center gap-1">
            <Button
              variant={page === "概览" ? "secondary" : "ghost"}
              size="sm"
              className={cn(
                page === "概览" && "bg-primary/10 text-primary hover:bg-primary/15",
              )}
              asChild
            >
              <a href="/" onClick={(e) => navigate("/", e)}>
                <LayoutDashboard data-icon="inline-start" />
                概览
              </a>
            </Button>
            <Button
              variant={page === "账号详情" ? "secondary" : "ghost"}
              size="sm"
              className={cn(
                page === "账号详情" && "bg-primary/10 text-primary hover:bg-primary/15",
              )}
              asChild
            >
              <a href="/login" onClick={(e) => navigate("/login", e)}>
                账号详情
              </a>
            </Button>
            <Button
              variant={page === "CLI 更新" ? "secondary" : "ghost"}
              size="sm"
              className={cn(
                page === "CLI 更新" && "bg-primary/10 text-primary hover:bg-primary/15",
              )}
              asChild
            >
              <a href="/update" onClick={(e) => navigate("/update", e)}>
                CLI 更新
              </a>
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
      <main className="mx-auto flex w-full max-w-7xl flex-1 min-h-0 flex-col px-4 py-4 sm:px-6 sm:py-5 lg:overflow-hidden">
        {children}
      </main>
      <footer className="mx-auto flex w-full max-w-7xl shrink-0 items-center justify-between border-t border-border/50 px-4 py-2 text-xs text-muted-foreground sm:px-6">
        <span>agy-proxy</span>
        <span>Powered by Antigravity CLI</span>
      </footer>
    </div>
  );
}
function KeyManager({
  keys,
  loaded,
  showSkeleton,
  refresh,
}: {
  keys: Key[];
  loaded: boolean;
  showSkeleton: boolean;
  refresh: () => void;
}) {
  const [dialog, setDialog] = useState(false);
  const [editing, setEditing] = useState<Key | null>(null);
  const [deleting, setDeleting] = useState<Key | null>(null);
  const [name, setName] = useState("");
  const [secret, setSecret] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const slow = useDelayedSkeleton(!loaded, 2500);
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
      <Card className="flex flex-1 min-h-0 flex-col overflow-hidden border-border/80">
        <CardHeader className="shrink-0 gap-3 border-b border-border/50 bg-muted/20 py-3 sm:flex sm:flex-row sm:items-center sm:justify-between">
          <div className="flex flex-col gap-1">
            <CardTitle className="text-base">API 密钥</CardTitle>
            <CardDescription className="text-xs">
              为每个客户端分配独立密钥，随时管理访问权限。
            </CardDescription>
          </div>
          <Button size="sm" onClick={() => open()}>
            <Plus data-icon="inline-start" />
            创建密钥
          </Button>
        </CardHeader>
        <CardContent className="flex flex-1 min-h-0 flex-col overflow-y-auto pt-3">
          {!loaded ? (
            showSkeleton ? (
              <div className="flex flex-col gap-3">
                <LoadingStatus
                  label={slow ? "密钥读取较慢，请稍候…" : "正在读取 API 密钥…"}
                />
                <TableSkeleton
                  headers={[
                    "备注",
                    "密钥",
                    "请求",
                    "Tokens",
                    "最近使用",
                    "创建时间",
                    "操作",
                  ]}
                />
              </div>
            ) : (
              <div className="min-h-[200px] flex-1" />
            )
          ) : keys.length ? (
            <Table>
              <TableHeader className="sticky top-0 z-10 bg-card/95 backdrop-blur-xs">
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
                    <TableCell className="font-mono text-xs text-muted-foreground">
                      {k.masked}
                    </TableCell>
                    <TableCell className="tabular-nums">{number(k.requests)}</TableCell>
                    <TableCell className="tabular-nums">{number(k.usage.total_tokens)}</TableCell>
                    <TableCell className="text-xs text-muted-foreground">{date(k.last_used_at)}</TableCell>
                    <TableCell className="text-xs text-muted-foreground">{date(k.created_at)}</TableCell>
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
type CatalogModel = { id: string; label: string; family: string; enabled: boolean };
type ModelGroup = { model: string; label: string; enabled: boolean; variants: CatalogModel[] };

function Overview() {
  const [data, setData] = useState<Dashboard | null>(cachedDashboard);
  const [models, setModels] = useState<ModelGroup[]>(cachedModels ?? []);
  const [loaded, setLoaded] = useState(cachedDashboard !== null);
  const [modelsLoaded, setModelsLoaded] = useState(cachedModels !== null);
  const [tab, setTab] = useState(cachedOverviewTab);
  const [pendingModel, setPendingModel] = useState<string | null>(null);
  const modelRevision = useRef(0);
  const [error, setError] = useState("");
  const [modelError, setModelError] = useState("");
  const [query, setQuery] = useState("");
  const [version, setVersion] = useState(cachedUpdateVersion);
  const [tick, setTick] = useState(0);
  const [busy, setBusy] = useState(false);
  const showSkeleton = useDelayedSkeleton(!loaded);
  const showModelsSkeleton = useDelayedSkeleton(!modelsLoaded);
  const slowDashboard = useDelayedSkeleton(!loaded, 2500);
  const slowModels = useDelayedSkeleton(!modelsLoaded, 2500);
  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    async function load() {
      const revision = modelRevision.current;
      setBusy(true);
      try {
        const dashPromise = api<Dashboard>("/dashboard").then(
          (value) => {
            if (!alive) return;
            cachedDashboard = value;
            setData(value);
            setLoaded(true);
            setError("");
          },
          (reason) => {
            if (!alive) return;
            setError(message(reason));
            setLoaded(true);
          },
        );
        const modelsPromise = api<{ models: CatalogModel[] }>("/api/models").then(
          (value) => {
            if (!alive) return;
            if (revision === modelRevision.current) {
              const groups = new Map<string, ModelGroup>();
              for (const m of value.models) {
                const group = groups.get(m.family) || {
                  model: m.family,
                  label: m.label.replace(/\s*\((Low|Medium|High|XHigh|Max)\)$/i, ""),
                  enabled: m.enabled,
                  variants: [],
                };
                group.variants.push(m);
                groups.set(m.family, group);
              }
              const sorted = [...groups.values()].sort((a, b) =>
                a.model.localeCompare(b.model),
              );
              cachedModels = sorted;
              setModels(sorted);
              setModelError("");
            }
            setModelsLoaded(true);
          },
          (reason) => {
            if (!alive) return;
            setModelError(message(reason));
            setModelsLoaded(true);
          },
        );
        await Promise.allSettled([dashPromise, modelsPromise]);
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
      .then((s) => {
        cachedUpdateVersion = s.version;
        setVersion(s.version);
      })
      .catch(() => {});
  }, []);
  const refresh = () => setTick((t) => t + 1);
  async function toggleModel(model: ModelGroup, enabled: boolean) {
    if (pendingModel) return;
    modelRevision.current += 1;
    setPendingModel(model.model);
    try {
      const result = await api<{ family: string; enabled: boolean }>(
        `/api/models/${encodeURIComponent(model.model)}`,
        { method: "PATCH", body: JSON.stringify({ enabled }) },
      );
      setModels((current) => {
        const next = current.map((m) =>
          m.model === result.family ? { ...m, enabled: result.enabled } : m,
        );
        cachedModels = next;
        return next;
      });
      toast.success(`${model.model} 及其变体已${result.enabled ? "启用" : "禁用"}`);
    } catch (e) {
      toast.error(message(e));
    } finally {
      modelRevision.current += 1;
      setPendingModel(null);
      refresh();
    }
  }
  const u = data?.totals.usage || zero;
  const totalCacheRate = cacheRate(u);
  const filtered = models.filter((m) =>
    `${m.model} ${m.label} ${m.variants.map((v) => `${v.id} ${v.label}`).join(" ")}`
      .toLowerCase().includes(query.toLowerCase()),
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
      detail: "累计输入 Token 用量",
      icon: ArrowDownLeft,
    },
    {
      label: "输出 Tokens",
      value: u.output_tokens,
      detail: `思考 Tokens ${number(u.thinking_tokens)}`,
      icon: ArrowUpRight,
    },
    {
      label: "总缓存",
      value: u.cache_read_tokens,
      detail:
        totalCacheRate === null
          ? "累计缓存命中 Tokens"
          : `缓存命中率 ${totalCacheRate.toFixed(1)}%`,
      icon: Database,
    },
  ];
  return (
    <Shell page="概览">
      <div className="flex flex-1 min-h-0 flex-col gap-3.5">
        <div className="flex shrink-0 flex-wrap items-center justify-between gap-3">
          <div className="flex flex-col gap-1">
            <div className="flex items-center gap-2.5">
              <h1 className="text-xl font-semibold tracking-tight sm:text-2xl">
                工作空间概览
              </h1>
              {version && (
                <Badge variant="outline" className="border-primary/20 bg-primary/5 text-primary">
                  agy v{version}
                </Badge>
              )}
            </div>
            <p className="text-xs text-muted-foreground">
              查看模型用量，管理你的 API 连接。
            </p>
          </div>
          <Button variant="outline" size="sm" onClick={refresh} disabled={busy}>
            <RefreshCw
              data-icon="inline-start"
              className={cn(busy && "animate-spin")}
            />
            刷新数据
          </Button>
        </div>
        <ErrorNotice error={error} />
        <div className="grid shrink-0 grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-5">
          {metrics.map((m) => (
            <Card key={m.label} className="gap-1.5 border-border/80 py-3">
              <CardHeader className="flex flex-row items-center justify-between px-4 py-0">
                <CardDescription className="text-xs font-medium">
                  {m.label}
                </CardDescription>
                <span className="flex size-7 items-center justify-center rounded-md bg-primary/10 text-primary">
                  <m.icon className="size-3.5" />
                </span>
              </CardHeader>
              <CardContent className="px-4 py-0">
                {!loaded && showSkeleton ? (
                  <Skeleton className="h-7 w-20" />
                ) : (
                  <div className="text-2xl font-semibold tracking-tight tabular-nums">
                    {number(m.value)}
                  </div>
                )}
                <p className="mt-1 truncate text-[11px] text-muted-foreground">
                  {m.detail}
                </p>
              </CardContent>
            </Card>
          ))}
        </div>
        <Tabs
          value={tab}
          onValueChange={(v) => {
            cachedOverviewTab = v;
            setTab(v);
          }}
          className="flex flex-1 min-h-0 flex-col gap-3"
        >
          <div className="flex shrink-0 flex-wrap items-center justify-between gap-2">
            <TabsList className="border border-border/60 bg-muted/80">
              <TabsTrigger value="models">模型列表</TabsTrigger>
              <TabsTrigger value="keys">API 密钥</TabsTrigger>
              <TabsTrigger value="recent">最近请求</TabsTrigger>
            </TabsList>
            <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <span
                className={cn(
                  "size-1.5 rounded-full",
                  error
                    ? "bg-destructive"
                    : busy
                      ? "bg-primary animate-pulse"
                      : "bg-emerald-500",
                )}
              />
              {error ? "连接中断" : busy ? "同步中…" : "每 5 秒自动同步"}
            </span>
          </div>
          <TabsContent value="models" className="flex flex-1 min-h-0 flex-col">
            <Card className="flex flex-1 min-h-0 flex-col overflow-hidden border-border/80">
              <CardHeader className="shrink-0 gap-3 border-b border-border/50 bg-muted/20 py-3 sm:flex sm:flex-row sm:items-center sm:justify-between">
                <div className="flex flex-col gap-1">
                  <CardTitle className="text-base">
                    模型列表{" "}
                    <span className="ml-1.5 text-xs font-normal text-muted-foreground">
                      {models.length}
                    </span>
                  </CardTitle>
                  <CardDescription className="text-xs">
                    按基础模型统一启用或禁用，包含所有变体。禁用后无法调用，也不会出现在客户端同步列表中。
                  </CardDescription>
                </div>
                <div className="w-full sm:w-60">
                  <Input
                    aria-label="搜索模型"
                    placeholder="搜索模型名称…"
                    className="h-8 text-xs"
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                  />
                </div>
              </CardHeader>
              <CardContent className="flex flex-1 min-h-0 flex-col overflow-y-auto pt-3">
                <ErrorNotice error={modelError} />
                {!modelsLoaded ? (
                  showModelsSkeleton ? (
                    <div className="flex flex-col gap-3">
                      <LoadingStatus
                        label={
                          slowModels
                            ? "模型列表读取较慢，首次读取需要调用 agy，最长约 45 秒…"
                            : "正在读取模型列表…"
                        }
                      />
                      <ModelsSkeleton />
                    </div>
                  ) : (
                    <div className="min-h-[200px] flex-1" />
                  )
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
                    <TableHeader className="sticky top-0 z-10 bg-card/95 backdrop-blur-xs">
                      <TableRow>
                        {["模型", "包含变体", "启用"].map((h) => (
                          <TableHead key={h}>{h}</TableHead>
                        ))}
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {filtered.map((m) => (
                        <TableRow key={m.model}>
                          <TableCell>
                            <div className="flex items-center gap-2.5 py-0.5">
                              <div className="flex size-8 shrink-0 items-center justify-center rounded-lg border border-primary/15 bg-primary/5">
                                <Box className="size-3.5 text-primary" />
                              </div>
                              <div>
                                <div className="font-mono text-xs font-medium">
                                  {m.model}
                                </div>
                                <div className="mt-0.5 text-xs text-muted-foreground">
                                  {m.label || "Antigravity 模型"}
                                </div>
                              </div>
                            </div>
                          </TableCell>
                          <TableCell className="whitespace-normal">
                            <div className="flex flex-wrap gap-1.5">
                              {m.variants.map((v) => (
                                <Badge key={v.id} variant="outline" title={v.id}>
                                  {v.id === m.model ? "默认" : v.id.slice(m.model.length + 1)}
                                </Badge>
                              ))}
                            </div>
                          </TableCell>
                          <TableCell>
                            <div className="flex items-center gap-2.5">
                              <Switch
                                aria-label={`启用 ${m.model} 及其所有变体`}
                                checked={m.enabled}
                                disabled={pendingModel !== null}
                                onCheckedChange={(enabled) => toggleModel(m, enabled)}
                              />
                              <span className="text-xs text-muted-foreground">
                                {pendingModel === m.model ? "保存中…" : m.enabled ? "已启用" : "已禁用"}
                              </span>
                            </div>
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                )}
              </CardContent>
            </Card>
          </TabsContent>
          <TabsContent value="keys" className="flex flex-1 min-h-0 flex-col">
            <KeyManager
              keys={data?.keys ?? []}
              loaded={loaded}
              showSkeleton={showSkeleton}
              refresh={refresh}
            />
          </TabsContent>
          <TabsContent value="recent" className="flex flex-1 min-h-0 flex-col">
            <Card className="flex flex-1 min-h-0 flex-col overflow-hidden border-border/80">
              <CardHeader className="shrink-0 gap-1 border-b border-border/50 bg-muted/20 py-3">
                <CardTitle className="text-base">最近请求</CardTitle>
                <CardDescription className="text-xs">
                  最近 {data?.recent.length || 0} 条 / 共{" "}
                  {data?.recent_total || 0} 条调用记录。
                </CardDescription>
              </CardHeader>
              <CardContent className="flex flex-1 min-h-0 flex-col overflow-y-auto pt-3">
                {!loaded ? (
                  showSkeleton ? (
                    <div className="flex flex-col gap-3">
                      <LoadingStatus
                        label={
                          slowDashboard
                            ? "调用记录读取较慢，请稍候…"
                            : "正在读取调用记录…"
                        }
                      />
                      <TableSkeleton
                        headers={[
                          "时间",
                          "模型",
                          "账号",
                          "密钥",
                          "状态",
                          "耗时",
                          "用量",
                        ]}
                      />
                    </div>
                  ) : (
                    <div className="min-h-[200px] flex-1" />
                  )
                ) : !data?.recent.length ? (
                  <Blank
                    title="还没有请求记录"
                    description="通过 API 发起调用后，在这里查看状态、耗时与用量。"
                  />
                ) : (
                  <Table>
                    <TableHeader className="sticky top-0 z-10 bg-card/95 backdrop-blur-xs">
                      <TableRow>
                        {[
                          "时间",
                          "模型",
                          "账号",
                          "密钥",
                          "状态",
                          "耗时",
                          "用量",
                        ].map((h) => (
                          <TableHead key={h}>{h}</TableHead>
                        ))}
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {data.recent.map((r, i) => (
                        <TableRow key={`${r.ts}-${i}`}>
                          <TableCell className="text-xs text-muted-foreground">{date(r.ts)}</TableCell>
                          <TableCell className="font-mono text-xs">
                            {r.model}
                          </TableCell>
                          <TableCell
                            className="max-w-[160px] truncate text-xs text-muted-foreground"
                            title={r.account_id || undefined}
                          >
                            {r.account_name || "—"}
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
                          <TableCell className="tabular-nums">
                            {r.duration_ms
                              ? (r.duration_ms / 1000).toFixed(1) + "s"
                              : "—"}
                          </TableCell>
                          <TableCell>
                            <RequestUsage usage={r.usage} />
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                )}
              </CardContent>
            </Card>
          </TabsContent>
        </Tabs>
      </div>
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
  accountQuery = "",
}: {
  state: BridgeState;
  onChange: (state: BridgeState) => void;
  accountQuery?: string;
}) {
  const [pending, setPending] = useState(false);
  const sending = useRef(false);
  async function send(key: string) {
    if (!state.interactive || sending.current) return;
    sending.current = true;
    setPending(true);
    try {
      const next = await api<BridgeState>("/login/input" + accountQuery, {
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
    <Card className="border-border/80">
      <CardHeader className="py-2.5">
        <CardTitle className="text-sm">CLI 交互终端</CardTitle>
        <CardDescription className="text-xs">
          查看当前提示，用下方按键选择登录方式或授权选项。回车会确认当前选项，请先核对内容。
        </CardDescription>
      </CardHeader>
      <CardContent className="flex min-w-0 flex-col gap-3">
        <div
          role="region"
          aria-label="CLI 当前画面"
          tabIndex={0}
          className="max-h-56 min-h-24 overflow-auto rounded-md border bg-muted/40 p-3 outline-offset-2"
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
          className="flex flex-wrap gap-1.5"
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
  accountId,
}: {
  update?: boolean;
  embedded?: boolean;
  accountId?: string;
}) {
  const base = update ? "/update" : "/login";
  const accountQuery = !update && accountId ? `?account_id=${encodeURIComponent(accountId)}` : "";
  const cacheKey = base + accountQuery;
  const title = update ? "CLI 更新" : "账号授权";
  const [state, setState] = useState<BridgeState | null>(
    () => cachedBridgeState.get(cacheKey) ?? null,
  );
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [code, setCode] = useState("");
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const d = await api<BridgeState>(base + "/status" + accountQuery);
        if (alive) {
          cachedBridgeState.set(cacheKey, d);
          if (update && d.version) cachedUpdateVersion = d.version;
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
  }, [base, tick, accountQuery, cacheKey, update]);
  async function action(path: string, body?: object) {
    setBusy(true);
    setError("");
    try {
      const d = await api<BridgeState>(base + path + accountQuery, {
        method: "POST",
        body: JSON.stringify({ ...body, session_id: state?.session_id }),
      });
      cachedBridgeState.set(cacheKey, d);
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
    <div className={cn("mx-auto flex w-full max-w-2xl flex-col gap-4", !embedded && "flex-1 min-h-0")}>
      {!embedded && (
        <div className="flex shrink-0 flex-wrap items-center justify-between gap-3">
          <div className="flex flex-col gap-1">
            <h1 className="text-xl font-semibold tracking-tight sm:text-2xl">{title}</h1>
            <p className="text-xs text-muted-foreground">
              {update
                ? "检查并更新 Antigravity CLI，无需重新部署服务。"
                : "连接 Google 账号，让你的应用开始使用 Antigravity 模型。"}
            </p>
          </div>
          <Button variant="ghost" size="sm" asChild>
            <a href="/" onClick={(e) => navigate("/", e)}>
              <ArrowLeft data-icon="inline-start" />
              返回概览
            </a>
          </Button>
        </div>
      )}
      <ErrorNotice error={error} />
      <Card className="shrink-0 border-border/80">
        <CardHeader>
          <div className="flex items-center justify-between">
            <CardTitle className="text-base">
              {update ? "Antigravity CLI" : "Antigravity 账号"}
            </CardTitle>
            <Badge
              variant={state?.state === "error" ? "destructive" : "secondary"}
            >
              {state ? label[state.state] || state.state : "尚未开始"}
            </Badge>
          </div>
          <CardDescription className="text-xs">
            {update
              ? `当前版本：${state?.version || cachedUpdateVersion ? "v" + (state?.version || cachedUpdateVersion) : "—"}`
              : "授权信息保存在当前服务，无需每次重新登录。"}
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3.5">
          {state?.message && (
            <p className="text-sm text-muted-foreground">{state.message}</p>
          )}
          {!update && (
            <ol className="grid grid-cols-1 gap-2 text-xs text-muted-foreground sm:grid-cols-3">
              {["确认 CLI 提示", "登录 Google 账号", "提交授权码"].map(
                (s, i) => (
                  <li key={s} className="flex items-center gap-2 rounded-lg border border-border/60 bg-muted/30 px-2.5 py-2">
                    <span className="flex size-5 shrink-0 items-center justify-center rounded-full bg-primary/10 text-[11px] font-medium text-primary">
                      {i + 1}
                    </span>
                    <span className="truncate">{s}</span>
                  </li>
                ),
              )}
            </ol>
          )}
          {state?.url && !update && (
            <div className="flex flex-wrap gap-2">
              <Button size="sm" asChild>
                <a
                  href={/^https:\/\//.test(state.url) ? state.url : "#"}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  打开 Google 授权页
                  <ExternalLink data-icon="inline-end" />
                </a>
              </Button>
              <Button size="sm" variant="outline" onClick={() => copyText(state.url!)}>
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
                  <Button size="sm" disabled={running || !code.trim()}>
                    提交授权码
                    <ArrowRight data-icon="inline-end" />
                  </Button>
                </FieldGroup>
              </form>
            )}
          {update && (
            <p className="text-xs leading-5 text-muted-foreground">
              更新会释放空闲会话；正在进行的请求会继续完成。
            </p>
          )}
        </CardContent>
        <CardFooter className="flex-wrap gap-2">
          {!update && state?.state === "authenticated" && (
            <Button size="sm" disabled={busy} onClick={() => action("/finish")}>
              完成并关闭
            </Button>
          )}
          <Button
            size="sm"
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
              size="sm"
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
        <LoginTerminal state={state} onChange={setState} accountQuery={accountQuery} />
      ) : null}
      {update && (state?.output || state?.tail) && (
        <Card className="flex flex-1 min-h-0 flex-col border-border/80">
          <CardHeader className="shrink-0 py-2.5">
            <CardTitle className="text-sm">运行日志</CardTitle>
          </CardHeader>
          <CardContent className="flex-1 min-h-0 overflow-hidden">
            <pre className="max-h-56 overflow-auto rounded-md bg-muted/50 p-3 font-mono text-xs leading-5 whitespace-pre-wrap break-all">
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
  const [path, setPath] = useState(() => location.pathname);
  useEffect(() => {
    const syncPath = () => setPath(location.pathname);
    window.addEventListener("popstate", syncPath);
    return () => window.removeEventListener("popstate", syncPath);
  }, []);
  return (
    <>
      {path === "/signin" ? (
        <SignIn />
      ) : path === "/login" ? (
        <Shell page="账号详情">
          <Accounts login={(id) => <Bridge key={id} embedded accountId={id} />} />
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
