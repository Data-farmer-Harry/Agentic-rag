import { useState, type FormEvent } from "react";
import { Check, LoaderCircle, UserRound, X } from "lucide-react";
import type { PersonaProfile } from "../types";

interface OnboardingDialogProps {
  persona: PersonaProfile;
  onComplete: (patch: object) => Promise<void>;
  onLater: () => void;
}

const tones = [
  ["warm", "温和"],
  ["direct", "直接"],
  ["analytical", "分析型"],
  ["concise", "简洁"]
] as const;

export function OnboardingDialog({
  persona,
  onComplete,
  onLater
}: OnboardingDialogProps) {
  const [displayName, setDisplayName] = useState(persona.user_display_name);
  const [description, setDescription] = useState(persona.self_description);
  const [tone, setTone] = useState(persona.preferred_tone || "warm");
  const [interests, setInterests] = useState(persona.interests.join(", "));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();

  async function submit(event: FormEvent) {
    event.preventDefault();
    if ((!displayName.trim() && !description.trim()) || busy) return;
    setBusy(true);
    setError(undefined);
    try {
      await onComplete({
        user_display_name: displayName.trim(),
        self_description: description.trim(),
        preferred_tone: tone,
        interests: interests
          .split(/[,，\n]/)
          .map((item) => item.trim())
          .filter(Boolean),
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || persona.timezone,
        complete_onboarding: true,
        expected_version: persona.version
      });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "保存失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="onboarding-layer" role="presentation">
      <form className="onboarding-dialog" onSubmit={submit}>
        <header>
          <div className="onboarding-heading">
            <span className="onboarding-mark"><UserRound size={19} /></span>
            <div>
              <small>Personal context</small>
              <h1>先认识一下</h1>
            </div>
          </div>
          <button type="button" className="icon-button" title="稍后设置" onClick={onLater}>
            <X size={16} />
          </button>
        </header>

        <div className="onboarding-fields">
          <label className="form-field">
            <span>怎么称呼你</span>
            <input
              autoFocus
              maxLength={100}
              value={displayName}
              onChange={(event) => setDisplayName(event.target.value)}
              placeholder="你的名字或称呼"
            />
          </label>
          <label className="form-field">
            <span>你现在主要在做什么</span>
            <textarea
              rows={3}
              maxLength={5_000}
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              placeholder="例如：计算机专业学生，正在研究 Agent 与知识图谱"
            />
          </label>
          <fieldset className="onboarding-tone">
            <legend>偏好语气</legend>
            <div className="segmented-control">
              {tones.map(([value, label]) => (
                <button
                  type="button"
                  key={value}
                  className={tone === value ? "is-selected" : ""}
                  onClick={() => setTone(value)}
                >
                  {label}
                </button>
              ))}
            </div>
          </fieldset>
          <label className="form-field">
            <span>关注方向</span>
            <input
              value={interests}
              onChange={(event) => setInterests(event.target.value)}
              placeholder="LLM, Agent, RAG, 软件工程"
            />
          </label>
        </div>

        {error && <div className="error-banner">{error}</div>}

        <footer>
          <button type="button" className="text-button" onClick={onLater}>稍后</button>
          <button
            className="primary-button"
            disabled={busy || (!displayName.trim() && !description.trim())}
          >
            {busy ? <LoaderCircle size={15} className="spin" /> : <Check size={15} />}
            保存并开始
          </button>
        </footer>
      </form>
    </div>
  );
}
