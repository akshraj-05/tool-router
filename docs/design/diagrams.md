# Architecture Diagram — Intelligent Tool Router (Routing-as-a-Service)

Render on GitLab/GitHub, VS Code Mermaid preview, or https://mermaid.live

```mermaid
flowchart TB

  classDef client fill:#0f2b46,stroke:#4aa3ff,stroke-width:2px,color:#eaf4ff;
  classDef gate fill:#123a52,stroke:#38bdf8,stroke-width:2px,color:#e6faff;
  classDef store fill:#1e2a4a,stroke:#8b9dff,stroke-width:2px,color:#eef1ff;
  classDef free fill:#123d24,stroke:#4ade80,stroke-width:2px,color:#eafff0;
  classDef rag fill:#0e3b2e,stroke:#2dd4bf,stroke-width:3px,color:#e7fffb;
  classDef dec fill:#3a2f0e,stroke:#facc15,stroke-width:2px,color:#fffbe6;
  classDef llm fill:#3a1e0e,stroke:#fb923c,stroke-width:2px,color:#fff2e6;
  classDef ext fill:#3a0e28,stroke:#f472b6,stroke-width:2px,color:#ffe9f5;
  classDef out fill:#2a0e3a,stroke:#c084fc,stroke-width:3px,color:#f6e9ff;

  Q["🧑‍💻 Client agent<br/><b>route this query</b>"]:::client

  subgraph SVC["⚙️  ROUTING-AS-A-SERVICE"]
    direction TB
    GW["🌐 API Gateway<br/>/registry · /route · /health"]:::gate

    subgraph ONB["📥 ONBOARD — validate + index the registry"]
      direction LR
      GRD["🛡️ Guardrail<br/>reject bad tools"]:::gate
      IDX[("🗂️ Embedded JSON store<br/>embeddings · clusters<br/>dup & version maps")]:::store
      GRD --> IDX
    end

    subgraph RET["🔍 RETRIEVAL — fused local scoring"]
      direction TB
      R["dense embeddings + BM25 lexical<br/>+ capability match − penalties<br/><b>one fused score, all tools</b>"]:::rag
      D["🎯 dedup + dynamic-K"]:::free
      R --> D
    end

    GATE{"❓ confident?"}:::dec
    HYDE["🧪 conditional HyDE<br/>only if confidence is low<br/><b>1 LLM call</b>"]:::llm
    PLAN["🤖 LLM: final rank + select + order<br/><b>order + repeat + version + refuse + scope</b><br/>1 call"]:::llm
    SCOPE{"🧭 out_of_scope<br/>verdict?"}:::dec
    CAP{"🧩 covers needed<br/>capability?"}:::dec
    WID["↩️ widen + fallback"]:::llm
    CLR["🙋 clarify / refuse / out-of-scope"]:::out

    GW --> GRD
    GW --> R
    IDX -.->|"indexed tools"| R
    D --> GATE
    GATE -->|"low, LLM available"| HYDE --> D
    GATE -->|"too low, no LLM"| CLR
    GATE -->|"confident"| PLAN --> SCOPE
    SCOPE -->|"yes"| CLR
    SCOPE -->|"no"| CAP
    CAP -->|"no"| WID --> PLAN
  end

  BR["☁️ Generation LLM<br/>(default: none — local only · Gemini live-verified)"]:::ext
  OUT["✅ selected tools + ordered plan<br/><b>~90–98% fewer tokens</b>"]:::out

  Q ==> GW
  HYDE -.->|"hypothetical-doc call"| BR
  PLAN -.->|"final rank + plan"| BR
  CAP ==>|"yes"| OUT
  OUT ==> Q
```

### Legend
- 🟢 **Green** = local & free (no LLM cost)  ·  🟩 **Teal** = the fused retrieval core
- 🟠 **Orange** = LLM call (conditional HyDE, or the required final-plan call)  ·  🟡 **Yellow** = decision gate
- 🔵 **Blue** = gateway / index  ·  🟣 **Purple** = result  ·  🩷 **Pink** = external provider

### "Is this just RAG?" — the one-line answer
The **retrieval core is RAG applied to tools** (embed tool schemas, retrieve the relevant
ones), fused with lexical (BM25) and capability signals into one score — there is **no
separate re-rank model stage**; that was considered in design but is not implemented (see
`engine-design.md`). We harden retrieval beyond vanilla RAG with **hybrid signals**,
**dynamic-K**, **dedup**, a **clarify/refuse/out-of-scope** path, **conditional HyDE** for
low-confidence queries, and an **LLM planner that also arbitrates scope** — so look-alike
tools, ordering, versioning, and vague queries are all handled, not just nearest-neighbor
lookup. The engine runs fully offline by default (no LLM); Gemini is the supported LLM
provider.
