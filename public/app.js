/* MF Facts UI: React 18 + htm (no build step). Renders /api/answer responses. */
const { useState, useRef, useEffect, useMemo } = React;
const html = htm.bind(React.createElement);

const MAX_CHARS = 300;
const EXAMPLES = [
  "What is the expense ratio of HDFC ELSS Tax Saver?",
  "What is the exit load of HDFC Mid Cap Fund?",
  "How do I download my capital gains statement?",
];
const LABELS = {
  answer: "Answer",
  refusal: "I can't help with that",
  clarify: "Need a bit more detail",
  unverifiable: "Couldn't verify this figure",
};
const WORD_DELAY_MS = 28;
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function Dots() {
  return html`<div class="dots" role="status" aria-label="Looking that up">
    <span></span><span></span><span></span>
  </div>`;
}

/* Words fade in one after another; screen readers get the full text at once. */
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
  const reveal = reduceMotion ? 0 : Math.min(count * WORD_DELAY_MS, 1400);
  const safeUrl = data.citation_url && data.citation_url.startsWith("https://");
  return html`
    <article class=${"card " + kind}>
      <span class="label">${LABELS[kind]}</span>
      <${Words} text=${data.text} />
      ${safeUrl && html`
        <p class="source" style=${{ "--d": reveal + "ms" }}>
          <a href=${data.citation_url} target="_blank" rel="noopener noreferrer">${data.citation_url}</a>
        </p>`}
      ${data.last_updated && html`
        <p class="meta" style=${{ "--d": reveal + 120 + "ms" }}>Last updated from sources: ${data.last_updated}</p>`}
    </article>`;
}

function App() {
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

  const pick = (text) => { setQuestion(text); ask(text); };
  const left = MAX_CHARS - question.length;

  return html`
    <main>
      <header>
        <span class="badge"><i></i>Facts-only · No investment advice</span>
        <h1>MF Facts · HDFC Mutual Fund</h1>
        <p class="sub">Ask about expense ratio, exit load, minimum SIP, lock-in, riskometer or statements.</p>
      </header>

      <div class="chips">
        ${EXAMPLES.map((text, i) => html`
          <button class="chip" key=${text} style=${{ "--i": i }} onClick=${() => pick(text)}>${text}</button>`)}
      </div>

      <form class="ask" onSubmit=${(e) => { e.preventDefault(); ask(question); }}>
        <input
          value=${question}
          maxLength=${MAX_CHARS}
          autoComplete="off"
          placeholder="Ask about expense ratio, exit load, lock-in…"
          aria-label="Your question"
          onInput=${(e) => setQuestion(e.target.value)} />
        <span class=${"count" + (left < 40 ? " near" : "")} aria-hidden="true">${left}</span>
        <button class="go" type="submit" disabled=${loading || !question.trim()}>${loading ? "…" : "Ask"}</button>
      </form>

      <section class="stage" aria-live="polite">
        ${loading ? html`<${Dots} />` : result && html`<${AnswerCard} key=${seq} data=${result} />`}
      </section>

      <p class="foot">Facts-only. No investment advice.</p>
    </main>`;
}

ReactDOM.createRoot(document.getElementById("root")).render(html`<${App} />`);
