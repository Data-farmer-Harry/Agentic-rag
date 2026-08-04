import { useCallback, useEffect, useState, type FormEvent } from "react";
import {
  BatteryMedium,
  BrainCircuit,
  Check,
  Gauge,
  LoaderCircle,
  RotateCcw,
  Save,
  SlidersHorizontal,
  UserRound
} from "lucide-react";
import { api } from "../api";
import type { EmotionSnapshot, EmotionState, PersonaProfile } from "../types";

const emotionLabels: Record<EmotionState, string> = {
  calm: "平静",
  focused: "专注",
  curious: "好奇",
  supportive: "支持",
  celebrating: "庆祝",
  reflective: "反思",
  resting: "休整"
};

export function ProfileView() {
  const [persona, setPersona] = useState<PersonaProfile>();
  const [emotion, setEmotion] = useState<EmotionSnapshot>();
  const [form, setForm] = useState({
    user_display_name: "",
    agent_name: "HermesGraph",
    self_description: "",
    communication_style: "clear and collaborative",
    preferred_tone: "warm",
    locale: "zh-CN",
    timezone: "Asia/Shanghai",
    interests: "",
    boundaries: ""
  });
  const [emotionState, setEmotionState] = useState<EmotionState>("calm");
  const [emotionNote, setEmotionNote] = useState("");
  const [emotionMinutes, setEmotionMinutes] = useState(120);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();

  const load = useCallback(async () => {
    const [nextPersona, nextEmotion] = await Promise.all([api.persona(), api.emotion()]);
    setPersona(nextPersona);
    setEmotion(nextEmotion);
    setEmotionState(nextEmotion.state);
    setForm({
      user_display_name: nextPersona.user_display_name,
      agent_name: nextPersona.agent_name,
      self_description: nextPersona.self_description,
      communication_style: nextPersona.communication_style,
      preferred_tone: nextPersona.preferred_tone,
      locale: nextPersona.locale,
      timezone: nextPersona.timezone,
      interests: nextPersona.interests.join(", "),
      boundaries: nextPersona.boundaries.join("\n")
    });
  }, []);

  useEffect(() => {
    void load().catch((cause) =>
      setError(cause instanceof Error ? cause.message : "无法载入个人设置")
    );
  }, [load]);

  async function mutate(operation: () => Promise<unknown>) {
    setBusy(true);
    setError(undefined);
    try {
      await operation();
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "操作失败");
    } finally {
      setBusy(false);
    }
  }

  function values(value: string) {
    return value
      .split(/[,\n]/)
      .map((item) => item.trim())
      .filter(Boolean);
  }

  function savePersona(event: FormEvent) {
    event.preventDefault();
    void mutate(() =>
      api.updatePersona({
        ...form,
        interests: values(form.interests),
        boundaries: values(form.boundaries),
        complete_onboarding: true,
        expected_version: persona?.version
      })
    );
  }

  return (
    <section className="data-view personal-view profile-view">
      <header className="view-header">
        <div>
          <span className="eyebrow">Identity and expression</span>
          <h1>个人设置</h1>
        </div>
        {busy && <LoaderCircle className="spin" size={17} />}
      </header>

      {error && <div className="error-banner">{error}</div>}

      <div className="profile-layout">
        <form className="persona-form" onSubmit={savePersona}>
          <div className="section-heading">
            <UserRound size={17} />
            <div>
              <strong>人格与偏好</strong>
              <span>{persona?.onboarding_completed_at ? "已完成引导" : "待完成引导"}</span>
            </div>
          </div>

          <div className="form-grid two-columns">
            <label className="form-field">
              <span>你的称呼</span>
              <input
                value={form.user_display_name}
                onChange={(event) =>
                  setForm((current) => ({
                    ...current,
                    user_display_name: event.target.value
                  }))
                }
              />
            </label>
            <label className="form-field">
              <span>Agent 名称</span>
              <input
                value={form.agent_name}
                onChange={(event) =>
                  setForm((current) => ({ ...current, agent_name: event.target.value }))
                }
              />
            </label>
            <label className="form-field">
              <span>偏好语气</span>
              <select
                value={form.preferred_tone}
                onChange={(event) =>
                  setForm((current) => ({
                    ...current,
                    preferred_tone: event.target.value
                  }))
                }
              >
                <option value="warm">温和</option>
                <option value="direct">直接</option>
                <option value="analytical">分析型</option>
                <option value="concise">简洁</option>
              </select>
            </label>
            <label className="form-field">
              <span>沟通风格</span>
              <input
                value={form.communication_style}
                onChange={(event) =>
                  setForm((current) => ({
                    ...current,
                    communication_style: event.target.value
                  }))
                }
              />
            </label>
            <label className="form-field">
              <span>语言</span>
              <input
                value={form.locale}
                onChange={(event) =>
                  setForm((current) => ({ ...current, locale: event.target.value }))
                }
              />
            </label>
            <label className="form-field">
              <span>时区</span>
              <input
                value={form.timezone}
                onChange={(event) =>
                  setForm((current) => ({ ...current, timezone: event.target.value }))
                }
              />
            </label>
          </div>
          <label className="form-field">
            <span>自我描述</span>
            <textarea
              rows={4}
              value={form.self_description}
              onChange={(event) =>
                setForm((current) => ({
                  ...current,
                  self_description: event.target.value
                }))
              }
            />
          </label>
          <div className="form-grid two-columns">
            <label className="form-field">
              <span>兴趣</span>
              <textarea
                rows={4}
                value={form.interests}
                onChange={(event) =>
                  setForm((current) => ({ ...current, interests: event.target.value }))
                }
              />
            </label>
            <label className="form-field">
              <span>边界</span>
              <textarea
                rows={4}
                value={form.boundaries}
                onChange={(event) =>
                  setForm((current) => ({ ...current, boundaries: event.target.value }))
                }
              />
            </label>
          </div>
          <button className="primary-button" disabled={busy}>
            {persona?.onboarding_completed_at ? <Save size={15} /> : <Check size={15} />}
            {persona?.onboarding_completed_at ? "保存设置" : "完成引导"}
          </button>
        </form>

        <section className="emotion-panel">
          <div className="section-heading">
            <BrainCircuit size={17} />
            <div>
              <strong>表达状态</strong>
              <span>{emotion ? emotionLabels[emotion.state] : "载入中"}</span>
            </div>
          </div>

          {emotion && (
            <div className="emotion-snapshot">
              <div className="emotion-state-mark">
                <Gauge size={20} />
                <strong>{emotion.label}</strong>
                {emotion.overridden && <span>手动</span>}
              </div>
              <p>{emotion.expression_hint}</p>
              <div className="emotion-meters">
                <label>
                  <span>倾向</span>
                  <i><b style={{ width: `${((emotion.valence + 1) / 2) * 100}%` }} /></i>
                </label>
                <label>
                  <span>能量</span>
                  <i><b style={{ width: `${emotion.energy * 100}%` }} /></i>
                </label>
              </div>
              <div className="reason-codes">
                {emotion.reason_codes.map((reason) => <span key={reason}>{reason}</span>)}
              </div>
            </div>
          )}

          <div className="emotion-options">
            {(Object.keys(emotionLabels) as EmotionState[]).map((state) => (
              <button
                key={state}
                className={emotionState === state ? "is-selected" : ""}
                onClick={() => setEmotionState(state)}
              >
                <span className={`emotion-swatch emotion-${state}`} />
                {emotionLabels[state]}
              </button>
            ))}
          </div>
          <label className="form-field">
            <span>状态备注</span>
            <input
              value={emotionNote}
              onChange={(event) => setEmotionNote(event.target.value)}
            />
          </label>
          <label className="range-field">
            <span><BatteryMedium size={14} /> 持续 {emotionMinutes} 分钟</span>
            <input
              type="range"
              min={30}
              max={1440}
              step={30}
              value={emotionMinutes}
              onChange={(event) => setEmotionMinutes(Number(event.target.value))}
            />
          </label>
          <div className="emotion-actions">
            <button
              className="primary-button"
              onClick={() =>
                void mutate(() =>
                  api.setEmotion(emotionState, emotionNote, emotionMinutes)
                )
              }
            >
              <SlidersHorizontal size={15} />
              应用状态
            </button>
            <button
              className="text-button"
              disabled={!emotion?.overridden}
              onClick={() => void mutate(() => api.clearEmotion())}
            >
              <RotateCcw size={14} />
              自动
            </button>
          </div>
        </section>
      </div>
    </section>
  );
}
