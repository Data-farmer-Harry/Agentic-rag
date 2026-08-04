import { useEffect, useRef, useState, type FormEvent } from "react";
import { Eye, EyeOff, KeyRound, LoaderCircle, ShieldCheck } from "lucide-react";

interface AuthDialogProps {
  checking: boolean;
  error?: string;
  onAuthenticate: (token: string) => Promise<void>;
}

export function AuthDialog({ checking, error, onAuthenticate }: AuthDialogProps) {
  const [token, setToken] = useState("");
  const [visible, setVisible] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!checking) inputRef.current?.focus();
  }, [checking]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!token.trim() || submitting) return;
    setSubmitting(true);
    try {
      await onAuthenticate(token);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="auth-layer" role="presentation">
      <form className="auth-dialog" onSubmit={submit} aria-label="工作区身份验证">
        <header>
          <span className="auth-mark"><ShieldCheck size={20} /></span>
          <div>
            <small>HermesGraph</small>
            <h1>{checking ? "正在验证会话" : "连接工作区"}</h1>
          </div>
        </header>
        {checking ? (
          <div className="auth-checking" aria-live="polite">
            <LoaderCircle className="spin" size={20} />
            <span>正在确认访问权限</span>
          </div>
        ) : (
          <>
            <div className="auth-body">
              <label htmlFor="api-access-token">访问令牌</label>
              <div className="auth-token-field">
                <KeyRound size={16} />
                <input
                  ref={inputRef}
                  id="api-access-token"
                  type={visible ? "text" : "password"}
                  value={token}
                  onChange={(event) => setToken(event.target.value)}
                  autoComplete="current-password"
                  spellCheck={false}
                  placeholder="Bearer token"
                  required
                />
                <button
                  type="button"
                  onClick={() => setVisible((current) => !current)}
                  title={visible ? "隐藏令牌" : "显示令牌"}
                >
                  {visible ? <EyeOff size={16} /> : <Eye size={16} />}
                </button>
              </div>
              {error && <div className="auth-error" role="alert">{error}</div>}
            </div>
            <footer>
              <button className="primary-button" type="submit" disabled={!token.trim() || submitting}>
                {submitting ? <LoaderCircle className="spin" size={15} /> : <KeyRound size={15} />}
                <span>{submitting ? "正在连接" : "进入工作区"}</span>
              </button>
            </footer>
          </>
        )}
      </form>
    </div>
  );
}
