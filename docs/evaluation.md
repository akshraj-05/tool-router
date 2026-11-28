# Evaluation

The test data and the scoring command are in [`eval/`](../eval/). A request passes only if the answer is exactly right: the expected tools in the expected order, a clarifying question, or nothing.

| Test set | Tools | Requests | Tool definitions from |
|---|---:|---:|---|
| `github` | 84 | 81 | GitHub MCP server |
| `playwright` | 17 | 19 | Playwright MCP server |
| `custom` | 63 | 120 | Written for this repository |

See [sources and licences](../eval/SOURCES.md). Each set is split into a tuning half and a final half.

## Results on the final halves, current code

| Setup | `github` (40) | `playwright` (10) | `custom` (60) | All (110) |
|---|---:|---:|---:|---:|
| Keyword search baseline | 18 (45%) | 6 (60%) | 20 (33%) | 44 (40%) |
| Local mode (no LLM, no key) | 25 (62%) | 8 (80%) | 39 (65%) | **72 (65%)** |
| Gemini planner | 37 (92%) | 10 (100%) | 57 (95%) | **104 (95%)** |

| Request type, all sets | Local mode | Gemini planner |
|---|---:|---:|
| One tool | 25/38 | 34/38 |
| Choosing between similar tools | 15/24 | 24/24 |
| Several tools in order | 12/20 | 19/20 |
| Current or old tool version (`custom` only) | 3/6 | 5/6 |
| Unclear request: should ask a question | 9/11 | 11/11 |
| No suitable tool: should return nothing | 8/11 | 11/11 |

The Gemini planner was measured on 2026-11-28 at commit `947784d`, with `gemini-3.5-flash-lite` and local vectors: one run of 125 calls, none failed. A 95% confidence interval for 104 of 110 is about 89% to 97%. A model's answers can differ between runs.

## What the results show

- **The LLM planner made the largest difference:** 104 of 110 against 72, mostly on similar tools (24/24 against 15/24) and multi-step requests (19/20 against 12/20). These requests were written with an AI assistant, which may favour an LLM (see the limits below).
- **Choosing one tool in local mode is not clearly better than keyword search.** On requests that expect exactly one tool:

  | | `github` | `playwright` | `custom` |
  |---|---:|---:|---:|
  | Keyword search | 18/26 | 6/6 | 20/36 |
  | Local mode | 17/26 | 5/6 | 21/36 |

  Local mode's advantage is asking questions, rejecting unrelated requests and planning several steps, which the baseline cannot do.
- **Speed and cost:** local mode answers in milliseconds. With Gemini a request took about 1 second and needs an API key.
- **Gemini embeddings** with the local planner scored about the same as local vectors on the tuning halves (earlier code), so they are not the recommended setup.

## Limits of these results

- The requests were written with an AI assistant and have not been independently reviewed. An LLM may find requests written this way easier than real users' requests, which would flatter the Gemini score.
- The sets are small, `playwright` especially: one request changes its final score by 10 points, and it has no unclear requests.
- Smaller catalogs are easier. Splitting GitHub and Playwright raised local scores on the same requests, from 30/50 to 33/50, without any change to the router.
- 20 of the 121 tools in those two servers were rejected by the router's validation and are not part of the test.
- Fixes are guided by the tuning halves only. The command does not list failures from the final halves.

## Run it yourself

```bash
pip install -e .
python eval/run.py --split final                         # local mode, GitHub set
python eval/run.py --set playwright --split final
python eval/run.py --set custom --split final
python eval/run.py --set github --split final --baseline

export GEMINI_API_KEY=your-key
TOOLSPOTTER_LLM_PROVIDER=gemini TOOLSPOTTER_LLM_MODEL=gemini-3.5-flash-lite \
  python eval/run.py --split final --from-env --delay 4.5
```

`--delay` keeps within free-tier rate limits. Failed Gemini calls are counted, and a run with failures is marked as not a clean score.

**Known causes of local-mode failures** (seen in the tuning halves)

- Local retrieval matches words, not meaning: "is web-01 up" finds no tool.
- The local planner finds steps at words such as "then", before a verb that starts a tool name, or where both sides of "and" clearly match different tools. It still loses steps when one side matches poorly.
- Similar tools are confused, for example reading an invoice instead of creating one.

Earlier development results came from a different 29-query suite on older code that is not in this repository, and are not comparable. See [the earlier evaluation report](eval_report.md).
