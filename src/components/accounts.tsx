import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  Activity,
  CircleHelp,
  Loader2,
  RefreshCw,
  ShieldCheck,
  UserRound,
} from "lucide-react";
import { api, date, message } from "@/lib/api";
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

type Account = {
  id: string;
  email: string | null;
  tier: string | null;
  status: string;
  message: string;
  checked_at: number;
};
type AccountUsage = Account & {
  usage_error: string | null;
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
    error || (data?.status !== "connected" ? data?.message : data?.usage_error);
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
      </div>
      <DialogFooter className="sm:items-center sm:justify-between">
        <span className="text-xs text-muted-foreground">
          {data?.usage
            ? `更新于 ${date(data.usage.fetched_at)}`
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

export function Accounts({ login }: { login: ReactNode }) {
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
            查看账号连接状态与可用额度。
          </p>
        </div>
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
      {error && <Failure text={error} />}
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
                </div>
                <CardTitle className="break-all">
                  {account.email || "Antigravity 账号"}
                </CardTitle>
                <CardDescription>
                  {account.tier || "当前服务账号"}
                </CardDescription>
              </CardHeader>
              <CardContent className="flex flex-col gap-3">
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
                        各模型组的剩余额度与刷新时间
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
                    {login}
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
