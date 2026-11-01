/* Nivesh UI: React 18 + htm (no build step). Renders /api/answer responses. */
const { useState, useRef, useEffect, useMemo } = React;
const html = htm.bind(React.createElement);

const MAX_CHARS = 300;
const SCHEMES = [
  { id: "large", cls: "s1", emoji: "🏛️", tag: "Blue-chip", name: "Large Cap", ask: "HDFC Large Cap Fund" },
  { id: "flexi", cls: "s2", emoji: "🧩", tag: "All-rounder", name: "Flexi Cap", ask: "HDFC Flexi Cap Fund" },
  { id: "elss", cls: "s3", emoji: "🧾", tag: "Tax saver", name: "ELSS", ask: "HDFC ELSS Tax Saver" },
  { id: "mid", cls: "s4", emoji: "🚀", tag: "Growth", name: "Mid Cap", ask: "HDFC Mid Cap Fund" },
];
const TOPICS = [
  { cls: "t1", icon: "💸", label: "Expense ratio", q: (s) => `What is the expense ratio of ${s}?` },
  { cls: "t2", icon: "🚪", label: "Exit load", q: (s) => `What is the exit load of ${s}?` },
  { cls: "t3", icon: "🌱", label: "Minimum SIP", q: (s) => `What is the minimum SIP of ${s}?` },
  { cls: "t4", icon: "🔒", label: "Lock-in", q: (s) => `What is the lock-in period of ${s}?` },
  { cls: "t5", icon: "🎯", label: "Riskometer", q: (s) => `What is the riskometer of ${s}?` },
  { cls: "t6", icon: "🧭", label: "Benchmark", q: (s) => `What is the benchmark of ${s}?` },
  { cls: "t7", icon: "📄", label: "Statements", q: () => "How do I download my capital gains statement?" },
];
const LABELS = {
  answer: "Answer",
  refusal: "I can't help with that",
  clarify: "Need a bit more detail",
  unverifiable: "Couldn't verify this figure",
};
const WORD_DELAY_MS = 30;
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function Skeleton() {
  return html`<div class="skeleton" role="status" aria-label="Looking that up"><i></i><i></i><i></i></div>`;
}

/* Words pop in one after another; screen readers get the full text at once. */
function Words({ text }) {
  const words = useMemo(() => text.split(/(\s+)/), [text]);
  let n = 0;
  return html`
    <p class="body">
      <span class="sr">${text}</span>
      <span aria-hidden="true">
        ${words.map((w, i) =>
          /^\s+$/.test(w)
            ? w
            : html`<span class="w" key=${i}
                style=${{ animationDelay: reduceMotion ? "0ms" : `${n++ * WORD_DELAY_MS}ms` }}>${w}</span>`
        )}
      </span>
    </p>`;
}

function AnswerCard({ data }) {
  const kind = LABELS[data.answer_type] ? data.answer_type : "clarify";
  const count = data.text.split(/\s+/).filter(Boolean).length;
  const reveal = reduceMotion ? 0 : Math.min(count * WORD_DELAY_MS, 1500);
  const safeUrl = data.citation_url && data.citation_url.startsWith("https://");
  return html`
    <article class=${"card " + kind}>
      <div class="pills">
        <span class="pill">${LABELS[kind]}</span>
        ${data.validated === true && html`<span class="pill soft">✓ Checked against the live page</span>`}
      </div>
      <${Words} text=${data.text} />
      ${safeUrl && html`
        <p class="source" style=${{ "--d": reveal + "ms" }}>
          <a href=${data.citation_url} target="_blank" rel="noopener noreferrer">↗ ${data.citation_url}</a>
        </p>`}
      ${data.last_updated && html`
        <p class="meta" style=${{ "--d": reveal + 120 + "ms" }}>Last updated from sources: ${data.last_updated}</p>`}
    </article>`;
}

function App() {
  const [scheme, setScheme] = useState(SCHEMES[0].id);
  const [question, setQuestion] = useState("");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState(null);
  const [seq, setSeq] = useState(0);
  const controller = useRef(null);

  useEffect(() => () => controller.current && controller.current.abort(), []);

  async function ask(raw) {
    const q = raw.trim();
    if (!q) return;
    if (controller.current) controller.current.abort();
    controller.current = new AbortController();
    setLoading(true);
    try {
      const res = await fetch("/api/answer", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: q }),
        signal: controller.current.signal,
      });
      const data = await res.json();
      if (res.status === 400) {
        setResult({ answer_type: "clarify", text: `Please enter a question of up to ${MAX_CHARS} characters.` });
      } else if (res.status === 503 || data.error) {
        setResult({ answer_type: "clarify", text: "The service is unavailable right now. Please try again shortly." });
      } else {
        setResult(data);
      }
      setSeq((s) => s + 1);
    } catch (err) {
      if (err.name === "AbortError") return;
      setResult({ answer_type: "clarify", text: "Couldn't reach the service. Please try again." });
      setSeq((s) => s + 1);
    } finally {
      setLoading(false);
    }
  }

  const current = SCHEMES.find((s) => s.id === scheme);
  const pick = (topic) => {
    const text = topic.q(current.ask);
    setQuestion(text);
    ask(text);
  };
  const left = MAX_CHARS - question.length;

  return html`
    <div class="mesh" aria-hidden="true">
      <span class="blob b1"></span><span class="blob b2"></span><span class="blob b3"></span><span class="blob b4"></span>
    </div>
    <main>
      <header class="hero">
        <div class="brand"><span class="logo">N</span><span class="wordmark">Nivesh</span></div>
        <span class="badge"><i></i>Facts-only · No investment advice</span>
        <h1>Know your fund,<br /><span class="grad">in one honest answer.</span></h1>
        <p class="sub">Official facts on four HDFC mutual fund schemes. Every answer comes with exactly one source link.</p>
      </header>

      <div class="step"><b>1</b>Pick a scheme</div>
      <div class="schemes">
        ${SCHEMES.map((s, i) => html`
          <button key=${s.id} class=${"scheme " + s.cls} style=${{ "--i": i }}
            aria-pressed=${scheme === s.id} onClick=${() => setScheme(s.id)}>
            <span class="emoji">${s.emoji}</span>
            <small>${s.tag}</small>
            <strong>${s.name}</strong>
          </button>`)}
      </div>

      <div class="step"><b>2</b>Tap what you want to know</div>
      <div class="topics">
        ${TOPICS.map((t, i) => html`
          <button key=${t.label} class=${"topic " + t.cls} style=${{ "--i": i }} onClick=${() => pick(t)}>
            <span class="ic">${t.icon}</span>${t.label}
          </button>`)}
      </div>

      <div class="step"><b>3</b>Or ask in your own words</div>
      <form class="ask" onSubmit=${(e) => { e.preventDefault(); ask(question); }}>
        <input
          value=${question}
          maxLength=${MAX_CHARS}
          autoComplete="off"
          placeholder="e.g. What is the exit load of HDFC Mid Cap?"
          aria-label="Your question"
          onInput=${(e) => setQuestion(e.target.value)} />
        <span class=${"count" + (left < 40 ? " near" : "")} aria-hidden="true">${left}</span>
        <button class="go" type="submit" disabled=${loading || !question.trim()}>${loading ? "…" : "Ask"}</button>
      </form>

      <section class="stage" aria-live="polite">
        ${loading ? html`<${Skeleton} />` : result && html`<${AnswerCard} key=${seq} data=${result} />`}
      </section>

      <p class="foot">Facts-only. No investment advice.</p>
    </main>`;
}

ReactDOM.createRoot(document.getElementById("root")).render(html`<${App} />`);
