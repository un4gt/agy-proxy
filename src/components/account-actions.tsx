import { useState } from "react";
import { Loader2, Pencil, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { api, message } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import {
  Dialog, DialogContent, DialogDescription, DialogFooter,
  DialogHeader, DialogTitle, DialogTrigger,
} from "@/components/ui/dialog";
import {
  AlertDialog, AlertDialogCancel, AlertDialogContent, AlertDialogDescription,
  AlertDialogFooter, AlertDialogHeader, AlertDialogTitle, AlertDialogTrigger,
} from "@/components/ui/alert-dialog";

type Props = {
  account: {
    id: string;
    name?: string;
    email: string | null;
    bound_clients?: number;
    disabled_models?: string[];
  };
  disabled: boolean;
  onUpdated: (name: string, disabledModels: string[]) => void;
  onDeleted: () => void;
};

export function AccountActions({ account, disabled, onUpdated, onDeleted }: Props) {
  const [editing, setEditing] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [name, setName] = useState("");
  const [disabledModels, setDisabledModels] = useState<string[]>([]);
  const [catalog, setCatalog] = useState<{ family: string; label: string }[]>([]);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [catalogError, setCatalogError] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const label = account.name || account.email || "Antigravity 账号";
  const path = `/api/accounts/${encodeURIComponent(account.id)}`;

  async function loadCatalog() {
    setCatalogLoading(true);
    setCatalogError("");
    try {
      const data = await api<{ models: { id: string; label: string; family: string }[] }>("/api/models");
      const groups = new Map<string, string>();
      for (const model of data.models) {
        if (!groups.has(model.family)) {
          groups.set(
            model.family,
            model.label.replace(/\s*\((Low|Medium|High|XHigh|Max)\)$/i, "") || model.family,
          );
        }
      }
      setCatalog(
        [...groups]
          .map(([family, modelLabel]) => ({ family, label: modelLabel }))
          .sort((a, b) => a.family.localeCompare(b.family)),
      );
    } catch (err) {
      setCatalogError(message(err));
    } finally {
      setCatalogLoading(false);
    }
  }

  function openEdit(open: boolean) {
    if (busy) return;
    setEditing(open);
    setError("");
    if (open) {
      setName(account.name || "");
      setDisabledModels([...(account.disabled_models || [])]);
      void loadCatalog();
    }
  }

  async function save() {
    if (!name.trim() || busy) return;
    setBusy(true);
    setError("");
    try {
      const result = await api<{ name: string; disabled_models: string[] }>(path, {
        method: "PATCH",
        body: JSON.stringify({ name: name.trim(), disabled_models: disabledModels }),
      });
      onUpdated(result.name, result.disabled_models || []);
      setEditing(false);
      toast.success("账号设置已更新");
    } catch (err) {
      setError(message(err));
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      const result = await api<{ warning?: string }>(path, { method: "DELETE" });
      setDeleting(false);
      if (result.warning) toast.warning(result.warning);
      else toast.success("账号已删除");
      onDeleted();
    } catch (err) {
      setError(message(err));
    } finally {
      setBusy(false);
    }
  }

  const failure = error && (
    <Alert variant="destructive">
      <AlertTitle>暂时无法完成操作</AlertTitle>
      <AlertDescription>{error}</AlertDescription>
    </Alert>
  );

  const rows = [...catalog];
  for (const family of disabledModels) {
    if (!rows.some((row) => row.family === family)) {
      rows.push({ family, label: family });
    }
  }
  rows.sort((a, b) => a.family.localeCompare(b.family));

  return (
    <div className="flex shrink-0 items-center gap-1">
      <Dialog open={editing} onOpenChange={openEdit}>
        <DialogTrigger asChild>
          <Button variant="ghost" size="icon-sm" disabled={disabled || busy}
            aria-label={`编辑账号 ${label}`} title="编辑账号">
            <Pencil />
          </Button>
        </DialogTrigger>
        <DialogContent showCloseButton={!busy} className="max-h-[88svh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>编辑账号</DialogTitle>
            <DialogDescription>修改备注,并设置该账号可以使用的模型。</DialogDescription>
          </DialogHeader>
          <form className="flex flex-col gap-5" onSubmit={(event) => { event.preventDefault(); void save(); }}>
            <FieldGroup>
              <Field data-disabled={busy}>
                <FieldLabel htmlFor={`edit-account-${account.id}`}>账号名称</FieldLabel>
                <Input id={`edit-account-${account.id}`} value={name} maxLength={100} required
                  disabled={busy} onChange={(event) => setName(event.target.value)} autoComplete="off" />
                <FieldDescription>最多 100 个字符。</FieldDescription>
              </Field>
              <Field data-disabled={busy}>
                <FieldLabel>可用模型</FieldLabel>
                <FieldDescription>
                  关闭的模型不会再用于此账号；全部开启表示不限制。按基础模型生效，包含该模型的全部思考档位。
                </FieldDescription>
                <div className="max-h-56 overflow-y-auto rounded-md border border-border/70">
                  {catalogLoading ? (
                    <div className="flex items-center gap-2 px-2.5 py-3 text-xs text-muted-foreground">
                      <Loader2 className="size-3.5 animate-spin" />
                      正在读取模型列表…
                    </div>
                  ) : catalogError ? (
                    <p className="px-2.5 py-3 text-xs text-destructive">{catalogError}</p>
                  ) : rows.length ? (
                    <div className="divide-y divide-border/60">
                      {rows.map((row) => (
                        <div key={row.family} className="flex items-center justify-between gap-3 px-2.5 py-2">
                          <div className="min-w-0">
                            <p className="truncate text-xs font-medium">{row.label}</p>
                            <p className="truncate font-mono text-[11px] text-muted-foreground">{row.family}</p>
                          </div>
                          <Switch
                            aria-label={`${row.label} 在此账号可用`}
                            checked={!disabledModels.includes(row.family)}
                            disabled={busy}
                            onCheckedChange={(value) =>
                              setDisabledModels((current) =>
                                value
                                  ? current.filter((family) => family !== row.family)
                                  : [...new Set([...current, row.family])],
                              )
                            }
                          />
                        </div>
                      ))}
                    </div>
                  ) : (
                    <p className="px-2.5 py-3 text-xs text-muted-foreground">暂无可配置的模型。</p>
                  )}
                </div>
                <p className="text-[11px] text-muted-foreground">
                  {disabledModels.length === 0
                    ? "当前未限制，所有模型都可用于此账号。"
                    : `当前已限制 ${disabledModels.length} 个模型。`}
                </p>
              </Field>
            </FieldGroup>
            {failure}
            <DialogFooter>
              <Button type="button" variant="outline" disabled={busy} onClick={() => setEditing(false)}>取消</Button>
              <Button type="submit" disabled={busy || !name.trim()}>
                {busy && <Loader2 data-icon="inline-start" className="animate-spin" />}
                {busy ? "正在保存…" : "保存设置"}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
      <AlertDialog open={deleting} onOpenChange={(open) => {
        if (!busy) { setDeleting(open); setError(""); }
      }}>
        <AlertDialogTrigger asChild>
          <Button variant="ghost" size="icon-sm" disabled={disabled || busy}
            aria-label={`删除账号 ${label}`} title="删除账号">
            <Trash2 />
          </Button>
        </AlertDialogTrigger>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>删除账号？</AlertDialogTitle>
            <AlertDialogDescription className="break-all">
              将删除「{label}」在本服务中的登录数据和固定安装标识，并解除客户端绑定。
              后续请求会重新选择可用账号；没有可用账号时将无法请求。此操作无法撤销。
            </AlertDialogDescription>
          </AlertDialogHeader>
          {!!account.bound_clients && (
            <p className="text-sm text-muted-foreground">当前有 {account.bound_clients} 个客户端绑定此账号。</p>
          )}
          {failure}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={busy}>取消</AlertDialogCancel>
            <Button variant="destructive" disabled={busy} onClick={() => void remove()}>
              {busy && <Loader2 data-icon="inline-start" className="animate-spin" />}
              {busy ? "正在删除…" : "确认删除"}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
