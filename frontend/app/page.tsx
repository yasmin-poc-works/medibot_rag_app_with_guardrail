"use client";

/** Client chat experience demonstrating login, RBAC messaging, citations, retrieval type, and guardrail status. */
import { FormEvent, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8001";
const accounts = ["dr.mehta", "nurse.priya", "billing.ravi", "tech.anand", "admin.sys"];

export default function Home() {
  /** Hold session, question, and response state for the demo UI. */
  const [username, setUsername] = useState("nurse.priya");
  const [password, setPassword] = useState("nurse123");
  const [session, setSession] = useState<any>(null);
  const [question, setQuestion] = useState("");
  const [response, setResponse] = useState<any>(null);
  const [error, setError] = useState("");

  /** Log in with the selected assignment demo account. */
  async function handleLogin(event: FormEvent) {
    event.preventDefault(); setError("");
    try {
      const result = await fetch(`${API_URL}/login`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username, password }) });
      if (!result.ok) { setError("Invalid username or password."); return; }
      setSession(await result.json()); setResponse(null); setQuestion("");
    } catch {
      setError("Could not reach the MediBot backend. Make sure it is running on port 8001.");
    }
  }

  /** End the current demo session so another account can be tested. */
  async function handleLogout() {
    const currentToken = session?.token;
    setError("");
    try {
      if (currentToken) {
        await fetch(`${API_URL}/logout`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token: currentToken }) });
      }
    } finally {
      setSession(null); setResponse(null); setQuestion("");
    }
  }

  /** Submit a question to the backend after RBAC-aware login. */
  async function handleChat(event: FormEvent) {
    event.preventDefault(); setError("");
    if (!question.trim()) { setError("Enter a question before asking MediBot."); return; }
    try {
      const result = await fetch(`${API_URL}/chat`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question: question.trim(), role: session.role }) });
      if (!result.ok) {
        const detail = await result.json().catch(() => null);
        setError(detail?.detail ?? "The assistant could not answer that question.");
        return;
      }
      setResponse(await result.json()); setQuestion("");
    } catch {
      setError("Could not reach the MediBot backend. Make sure it is running on port 8001.");
    }
  }

  return <main className="shell">
    <header><div><p className="eyebrow">MEDIASSIST HEALTH NETWORK</p><h1>MediBot</h1><p className="subtitle">Evidence-aware internal knowledge assistant</p></div>{session && <div className="session-controls"><div className="role-badge">{session.role}<small>active role</small></div><button type="button" className="logout" onClick={handleLogout}>Log out</button></div>}</header>
    <section className="card intro"><h2>Ask the clinical knowledge base</h2><p>Hybrid retrieval combines semantic search, BM25 keywords, and reranking. Analytical questions use SQL RAG only for permitted roles.</p></section>
    {!session ? <form className="card login" onSubmit={handleLogin}><h2>Demo login</h2><label>Account<select value={username} onChange={e => { setUsername(e.target.value); const found: any = { "dr.mehta": "doctor123", "nurse.priya": "nurse123", "billing.ravi": "billing123", "tech.anand": "tech123", "admin.sys": "admin123" }; setPassword(found[e.target.value]); }}>{accounts.map(account => <option key={account}>{account}</option>)}</select></label><label>Password<input value={password} onChange={e => setPassword(e.target.value)} /></label><button>Enter MediBot</button></form> : <><aside className="card access"><h3>Accessible collections</h3><div className="chips">{session.collections.map((collection: string) => <span key={collection}>{collection}</span>)}</div></aside><form className="card chat" onSubmit={handleChat}><label>Your question<textarea value={question} onChange={e => setQuestion(e.target.value)} placeholder="e.g. What are the infection-control precautions?" /></label><button>Ask MediBot</button></form>{response && <article className="card answer"><div className="answer-meta">{response.guardrail === "blocked" ? <span className="blocked">Safety policy</span> : <span>{response.retrieval_type === "sql_rag" ? "SQL RAG" : "Hybrid RAG"}</span>}<span>{response.role}</span></div><div className="markdown"><ReactMarkdown remarkPlugins={[remarkGfm]}>{response.answer}</ReactMarkdown></div>{response.sources?.length > 0 && <><h3>Sources</h3><ul>{response.sources.map((source: any, index: number) => <li key={index}><strong>{source.source_document}</strong> · {source.section_title} · {source.collection}</li>)}</ul></>}{response.request_id && <p className="request-id">Reference ID for support: <code>{response.request_id}</code></p>}</article>}</>}
    {error && <p className="error">{error}</p>}
  </main>;
}
