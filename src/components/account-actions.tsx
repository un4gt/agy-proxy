import { useState } from "react";
import { Loader2, Pencil, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { api, message } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
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
  account: { id: string; name?: string; email: string | null; bound_clients?: number };
  disabled: boolean;
  onRenamed: (name: string) => void;
  onDeleted: () => void;
};

export function AccountActions({ account, disabled, onRenamed, onDeleted }: Props) {
  const [editing, setEditing] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const label = account.name || account.email || "Antigravity 账号";
  const path = `/api/accounts/${encodeURIComponent(account.id)}`;

  async function save() {
    if (!name.trim() || busy) return;
    setBusy(true);
    setError("");
    try {
      const result = await api<{ name: string }>(path, {
        method: "PATCH", body: JSON.stringify({ name: name.trim() }),
      });
      onRenamed(result.name);
      setEditing(false);
      toast.success("账号名称已更新");
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

  return (
    <div className="flex shrink-0 items-center gap-1">
      <Dialog open={editing} onOpenChange={(open) => {
        if (busy) return;
        setEditing(open);
        setName(account.name || "");
        setError("");
      }}>
        <DialogTrigger asChild>
          <Button variant="ghost" size="icon-sm" disabled={disabled || busy}
            aria-label={`编辑账号 ${label}`} title="编辑账号">
            <Pencil />
          </Button>
        </DialogTrigger>
        <DialogContent showCloseButton={!busy}>
          <DialogHeader>
            <DialogTitle>编辑账号</DialogTitle>
            <DialogDescription>设置一个便于识别的名称，用于区分你的账号。</DialogDescription>
          </DialogHeader>
          <form className="flex flex-col gap-5" onSubmit={(event) => { event.preventDefault(); void save(); }}>
            <FieldGroup>
              <Field data-disabled={busy}>
                <FieldLabel htmlFor={`edit-account-${account.id}`}>账号名称</FieldLabel>
                <Input id={`edit-account-${account.id}`} value={name} maxLength={100} required
                  disabled={busy} onChange={(event) => setName(event.target.value)} autoComplete="off" />
                <FieldDescription>最多 100 个字符。</FieldDescription>
              </Field>
            </FieldGroup>
            {failure}
            <DialogFooter>
              <Button type="button" variant="outline" disabled={busy} onClick={() => setEditing(false)}>取消</Button>
              <Button type="submit" disabled={busy || !name.trim()}>
                {busy && <Loader2 data-icon="inline-start" className="animate-spin" />}
                {busy ? "正在保存…" : "保存名称"}
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
