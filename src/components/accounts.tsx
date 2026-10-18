import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  Activity,
  CircleHelp,
  Loader2,
  RefreshCw,
  ShieldCheck,
  UserRound,
  Plus,
} from "lucide-react";
import { api, date, message } from "@/lib/api";
import { AccountActions } from "@/components/account-actions";
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Progress } from "@/components/ui/progress";
import { Skeleton } from "@/components/ui/skeleton";
import { Input } from "@/components/ui/input";
import { Field, FieldGroup, FieldLabel, FieldDescription } from "@/components/ui/field";

type Account = {
  id: string;
  email: string | null;
  tier: string | null;
  status: string;
  message: string;
  checked_at: number;
  name?: string;
  isolated?: boolean;
  bound_clients?: number;
  exhausted_scopes?: string[];
};
type AccountUsage = Account & {
  usage_error: string | null;
  models_error: string | null;
  models: null | {
    id: string;
    name: string;
    remaining_fraction: number | null;
    reset_at: number | null;
  }[];
  usage: null | {
    fetched_at: number;
    groups: {
      name: string;
      models: string;
      buckets: {
        kind: string;
        label: string;
        remaining_fraction: number | null;
        reset_at: number | null;
      }[];
    }[];
  };
};
const statusLabels: Record<string, string> = {
  connected: "连接正常",
  disconnected: "未连接",
  unauthorized: "登录已失效",
  forbidden: "无访问权限",
  unavailable: "服务不可用",
  error: "连接检查失败",
};

function Failure({ text }: { text: string }) {
  return (
    <Alert variant="destructive">
      <CircleHelp />
      <AlertTitle>暂时无法获取数据</AlertTitle>
      <AlertDescription>{text}</AlertDescription>
    </Alert>
  );
}

function resetLabel(
  reset: number | null,
  now: number,
  remaining: number | null,
) {
  if (reset !== null) {
    const minutes = Math.ceil((reset - now) / 60);
    if (minutes <= 0) return "已到刷新时间，请刷新额度";
    return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分钟后刷新`;
  }
  return remaining === 1 ? "额度充足" : "上游未提供刷新时间";
}

function UsageDetails({
  accountId,
  onAccount,
}: {
  accountId: string;
  onAccount: (account: Account) => void;
}) {
  const [data, setData] = useState<AccountUsage | null>(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [now, setNow] = useState(Date.now() / 1000);
  const onAccountRef = useRef(onAccount);
  onAccountRef.current = onAccount;
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now() / 1000), 30_000);
    return () => clearInterval(timer);
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    setBusy(true);
    setError("");
    setData(null);
    api<AccountUsage>(
      `/api/accounts/${encodeURIComponent(accountId)}/usage${refresh ? "?refresh=true" : ""}`,
      { signal: controller.signal },
    )
      .then((result) => {
        setData(result);
        onAccountRef.current(result);
      })
      .catch((err) => {
        if (!controller.signal.aborted) setError(message(err));
      })
      .finally(() => {
        if (!controller.signal.aborted) setBusy(false);
      });
    return () => controller.abort();
  }, [accountId, refresh]);
  const failure =
    error || (data?.status !== "connected" ? data?.message : null);
  return (
    <>
      <div className="flex flex-col gap-4" aria-busy={busy}>
        {busy ? (
          <div
            className="flex flex-col gap-4"
            role="status"
            aria-label="正在查询额度"
          >
            <Skeleton className="h-40 w-full" />
            <Skeleton className="h-40 w-full" />
          </div>
        ) : failure ? (
          <Failure text={failure} />
        ) : (
          <>
            <Card>
              <CardHeader>
                <CardTitle>可用模型</CardTitle>
                <CardDescription>此账号当前返回的模型及各自剩余额度。</CardDescription>
              </CardHeader>
              <CardContent>
                {data?.models_error ? (
                  <Failure text={data.models_error} />
                ) : data?.models?.length ? (
                  <div className="grid gap-6 sm:grid-cols-2">
                    {data.models.map((model) => {
                      const percent = model.remaining_fraction === null
                        ? null : model.remaining_fraction * 100;
                      return (
                        <div key={model.id} className="flex min-w-0 flex-col gap-2">
                          <p className="break-words text-sm font-medium">{model.name}</p>
                          <p className="break-all text-xs text-muted-foreground">{model.id}</p>
                          <p className="text-sm tabular-nums">
                            {percent === null ? "额度未知" : `${percent.toFixed(2)}% 剩余`}
                          </p>
                          {percent !== null && (
                            <Progress value={percent} aria-label={`${model.name}剩余额度`} />
                          )}
                          <p className="text-xs text-muted-foreground" title={model.reset_at ? date(model.reset_at) : undefined}>
                            {resetLabel(model.reset_at, now, model.remaining_fraction)}
                          </p>
                        </div>
                      );
                    })}
                  </div>
                ) : (
                  <p className="text-sm text-muted-foreground">上游暂未返回可用模型，请稍后刷新。</p>
                )}
              </CardContent>
            </Card>
            <p className="text-sm font-medium">共享额度</p>
            {data?.usage_error ? (
              <Failure text={data.usage_error} />
            ) : data?.usage?.groups.length ? (
                data.usage.groups.map((group, index) => (
                  <Card key={`${group.name}-${index}`}>
                    <CardHeader>
                      <CardTitle>{group.name}</CardTitle>
                      {group.models && (
                        <CardDescription>{group.models}</CardDescription>
                      )}
                    </CardHeader>
                    <CardContent className="grid gap-6 sm:grid-cols-2">
                      {group.buckets.length ? (
                        group.buckets.map((bucket, i) => {
                          const label =
                            bucket.kind === "weekly"
                              ? "每周额度"
                              : bucket.kind === "5h"
                                ? "五小时额度"
                                : bucket.label;
                          const percent =
                            bucket.remaining_fraction === null
                              ? null
                              : bucket.remaining_fraction * 100;
                          return (
                            <div key={i} className="flex flex-col gap-3">
                              <div className="flex flex-wrap items-baseline justify-between gap-2">
                                <span className="text-sm text-muted-foreground">
                                  {label}
                                </span>
                                <span className="text-lg font-semibold tabular-nums">
                                  {percent === null
                                    ? "未知"
                                    : `${percent.toFixed(2)}%`}
                                  <span className="ml-1 text-xs font-normal text-muted-foreground">
                                    剩余
                                  </span>
                                </span>
                              </div>
                              {percent !== null && (
                                <Progress
                                  value={percent}
                                  aria-label={`${group.name} ${label}剩余`}
                                />
                              )}
                              <p
                                className="text-xs text-muted-foreground"
                                title={
                                  bucket.reset_at ? date(bucket.reset_at) : undefined
                                }
                              >
                                {resetLabel(
                                  bucket.reset_at,
                                  now,
                                  bucket.remaining_fraction,
                                )}
                              </p>
                            </div>
                          );
                        })
                      ) : (
                        <p className="text-sm text-muted-foreground">
                          上游暂未提供该组额度。
                        </p>
                      )}
                    </CardContent>
                  </Card>
                ))
              ) : (
                <Alert>
                  <CircleHelp />
                  <AlertTitle>暂无额度数据</AlertTitle>
                  <AlertDescription>
                    上游暂未返回此账号的额度，请稍后刷新。
                  </AlertDescription>
                </Alert>
              )}
          </>
        )}
      </div>
      <DialogFooter className="sm:items-center sm:justify-between">
        <span className="text-xs text-muted-foreground">
          {data?.status === "connected"
            ? `更新于 ${date(data.checked_at)}`
            : "查询账号的实时可用额度"}
        </span>
        <Button
          variant="outline"
          disabled={busy}
          onClick={() => setRefresh((v) => v + 1)}
        >
          {busy ? (
            <Loader2 data-icon="inline-start" className="animate-spin" />
          ) : (
            <RefreshCw data-icon="inline-start" />
          )}
          刷新额度
        </Button>
      </DialogFooter>
    </>
  );
}

function AddAccount({ onAdded }: { onAdded: () => void }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function add(importCurrent = false) {
    setBusy(true);
    setError("");
    try {
      if (file && file.size > 256 * 1024) throw new Error("凭据文件不能超过 256 KiB");
      let credentials;
      if (file && !importCurrent) {
        try { credentials = JSON.parse(await file.text()); }
        catch { throw new Error("凭据文件不是有效的 JSON"); }
      }
      await api("/api/accounts", {
        method: "POST",
        body: JSON.stringify({ name: name.trim() || "Antigravity 账号", credentials, import_current: importCurrent }),
      });
      setOpen(false);
      setName("");
      setFile(null);
      onAdded();
    } catch (e) { setError(message(e)); }
    finally { setBusy(false); }
  }
  return (
    <Dialog open={open} onOpenChange={(value) => { if (!busy) { setOpen(value); setError(""); setFile(null); } }}>
      <DialogTrigger asChild><Button><Plus data-icon="inline-start" />添加账号</Button></DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>添加隔离账号</DialogTitle>
          <DialogDescription>导入凭据，或创建账号后在卡片中完成授权。每个账号独立保存登录状态。</DialogDescription>
        </DialogHeader>
        <form onSubmit={(event) => { event.preventDefault(); void add(); }} className="flex flex-col gap-5">
          <FieldGroup>
            <Field>
              <FieldLabel htmlFor="account-name">账号名称</FieldLabel>
              <Input id="account-name" value={name} maxLength={100} disabled={busy} onChange={(e) => setName(e.target.value)} placeholder="例如：主账号" />
            </Field>
            <Field>
              <FieldLabel htmlFor="account-file">凭据文件（可选）</FieldLabel>
              <Input id="account-file" type="file" disabled={busy} onChange={(e) => setFile(e.target.files?.[0] || null)} />
              <FieldDescription>支持 agy 原生凭据或包含 token 的 agy-switch 账号 JSON。留空后可在 Linux / Docker 中交互登录。</FieldDescription>
            </Field>
          </FieldGroup>
          {error && <Failure text={error} />}
          <DialogFooter>
            <Button type="button" variant="outline" disabled={busy} onClick={() => void add(true)}>导入本机当前凭据</Button>
            <Button type="submit" disabled={busy}>{busy ? "正在保存…" : file ? "导入账号" : "创建账号"}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function Accounts({ login }: { login: (accountId: string) => ReactNode }) {
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setBusy(true);
    setError("");
    api<{ accounts: Account[] }>(
      `/api/accounts${tick ? "?refresh=true" : ""}`,
      { signal: controller.signal },
    )
      .then((data) => setAccounts(data.accounts))
      .catch((err) => {
        if (!controller.signal.aborted) setError(message(err));
      })
      .finally(() => {
        if (!controller.signal.aborted) setBusy(false);
      });
    return () => controller.abort();
  }, [tick]);
  function updateAccount(account: Account) {
    setAccounts((items) =>
      items.map((item) => (item.id === account.id ? account : item)),
    );
  }
  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div className="flex flex-col gap-2">
          <h1 className="text-2xl font-semibold tracking-tight">账号详情</h1>
          <p className="text-sm text-muted-foreground">
            同一客户端持续使用已绑定账号，仅在额度耗尽后自动切换。
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
        <AddAccount onAdded={() => setTick((v) => v + 1)} />
        <Button
          variant="outline"
          disabled={busy}
          onClick={() => setTick((v) => v + 1)}
        >
          {busy ? (
            <Loader2 data-icon="inline-start" className="animate-spin" />
          ) : (
            <RefreshCw data-icon="inline-start" />
          )}
          刷新状态
        </Button>
        </div>
      </div>
      {error && <Failure text={error} />}
      {!busy && !error && !accounts.length && (
        <Empty>
          <EmptyHeader>
            <EmptyMedia variant="icon"><UserRound /></EmptyMedia>
            <EmptyTitle>还没有账号</EmptyTitle>
            <EmptyDescription>添加账号并完成授权后，即可开始使用模型。</EmptyDescription>
          </EmptyHeader>
        </Empty>
      )}
      <div
        className="grid items-start gap-5 md:grid-cols-2 xl:grid-cols-3"
        aria-label="账号列表"
        aria-busy={busy}
      >
        {busy && !accounts.length ? (
          <Skeleton className="h-64 w-full" />
        ) : (
          accounts.map((account) => (
            <Card
              key={account.id}
              className="min-w-0"
              data-account-id={account.id}
            >
              <CardHeader>
                <div className="flex items-center justify-between gap-3">
                  <UserRound className="size-5 text-muted-foreground" />
                  <div className="flex items-center gap-2">
                  <Badge
                    variant={
                      busy || error
                        ? "outline"
                        : account.status === "connected"
                          ? "secondary"
                          : account.status === "disconnected"
                            ? "outline"
                            : "destructive"
                    }
                  >
                    {busy
                      ? "正在检查…"
                      : error
                        ? "状态未更新"
                        : statusLabels[account.status] || "状态未知"}
                  </Badge>
                  {account.isolated && <AccountActions account={account} disabled={busy}
                    onRenamed={(name) => setAccounts((items) => items.map((item) => item.id === account.id ? { ...item, name } : item))}
                    onDeleted={() => setAccounts((items) => items.filter((item) => item.id !== account.id))}
                  />}
                  </div>
                </div>
                <CardTitle className="break-all">
                  {account.email || account.name || "Antigravity 账号"}
                </CardTitle>
                <CardDescription>
                  {[account.name, account.tier].filter(Boolean).join(" · ") || "当前服务账号"}
                </CardDescription>
              </CardHeader>
              <CardContent className="flex flex-col gap-3">
                {account.isolated && <div className="flex flex-wrap gap-2">
                  <Badge variant="outline">独立账号环境</Badge>
                  {!!account.bound_clients && <Badge variant="secondary">已绑定请求</Badge>}
                  {!!account.exhausted_scopes?.length && <Badge variant="destructive">部分模型额度耗尽</Badge>}
                </div>}
                <p className="flex items-start gap-2 text-sm text-muted-foreground">
                  <ShieldCheck className="mt-0.5 size-4 shrink-0" />
                  {account.message}
                </p>
                <p className="text-xs text-muted-foreground">
                  检查于 {date(account.checked_at)}
                </p>
              </CardContent>
              <CardFooter className="flex-wrap gap-2">
                <Dialog>
                  <DialogTrigger asChild>
                    <Button
                      disabled={busy || account.status === "disconnected"}
                    >
                      <Activity data-icon="inline-start" />
                      查看 Usage
                    </Button>
                  </DialogTrigger>
                  <DialogContent className="max-h-[85svh] overflow-y-auto sm:max-w-2xl">
                    <DialogHeader>
                      <DialogTitle>Usage · 账号额度</DialogTitle>
                      <DialogDescription className="break-all">
                        {account.email || "Antigravity 账号"} ·
                        可用模型、共享额度与刷新时间
                      </DialogDescription>
                    </DialogHeader>
                    <UsageDetails
                      accountId={account.id}
                      onAccount={updateAccount}
                    />
                  </DialogContent>
                </Dialog>
                <Dialog
                  onOpenChange={(open) => {
                    if (!open) setTick((v) => v + 1);
                  }}
                >
                  <DialogTrigger asChild>
                    <Button variant="outline">
                      {account.status === "disconnected"
                        ? "连接账号"
                        : "登录操作"}
                    </Button>
                  </DialogTrigger>
                  <DialogContent className="max-h-[85svh] overflow-y-auto sm:max-w-3xl">
                    <DialogHeader>
                      <DialogTitle>账号授权</DialogTitle>
                      <DialogDescription>
                        按照 CLI 提示完成 Google 账号授权。
                      </DialogDescription>
                    </DialogHeader>
                    {login(account.id)}
                  </DialogContent>
                </Dialog>
              </CardFooter>
            </Card>
          ))
        )}
      </div>
    </div>
  );
}
