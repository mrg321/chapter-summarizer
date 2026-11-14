# Chapter Summarizer

> **Educational branch:** This repository is a branch of the Calibre
> **Chapter Summarizer** plugin, maintained here for educational and
> experimentation purposes. It is not the official upstream distribution and
> may diverge from it. For the original plugin, releases, and user support, see
> the [Chapter Summarizer MobileRead thread](https://www.mobileread.com/forums/showthread.php?t=374061).

Chapter Summarizer is a graphical plugin for [Calibre](https://calibre-ebook.com/)
that uses Calibre's configured AI providers to create book and document
summaries. The original plugin description calls out quick overviews and
chapter-level summaries.

## Features

- Generate a **flash overview** of a whole document or summarize selected
  chapters/sections individually.
- Split long chapters into chunks, summarize those chunks, and combine their
  results.
- Classify material (for example fiction, nonfiction, news, or reference) to
  guide summaries and highlights.
- Select an output language, retain the source language, and request language
  compatibility checks.
- Include a key event, idea, finding, or other highlight.
- Preview results and export them as Markdown, HTML, or EPUB.
- Optionally append summaries to Calibre comments, add them to book data files,
  or add them to the library as a new book.
- Cache completed chapter summaries and retain run history to help resume work.
- Use installed Calibre AI providers, plus two built-in rotating demo-provider
  pools. NVIDIA provider implementations are also included.

AI providers may send document excerpts or chapter contents to the selected
service. Review that provider's data-handling terms before summarizing
confidential material.

## Requirements

- Calibre **8.16.2 or newer** (the minimum declared by the plugin).
- Windows, macOS, or Linux.
- For cloud providers, the corresponding Calibre provider configuration and
  credentials. The local providers require their own local service to be
  running.

## Using the plugin

1. Install or open the plugin in Calibre and select a book in the library, or
   choose an external text/ebook file in the plugin.
2. Open **Chapter Summarizer**.
3. Choose a provider and model. Select chapters/sections for detailed summaries,
   or enable **Flash summary** for a whole-document overview.
4. Choose the output language and whether to include highlights, then run the
   summary.
5. Preview, save, or send the result to the desired Calibre destination.

The original plugin's MobileRead thread is the primary user-facing source for
installation packages, screenshots, release notes, and support. This source
tree does not include a plugin release archive or build script.

### Provider setup

- **Calibre AI providers:** Select a provider already installed and configured
  in Calibre's AI preferences (for example OpenRouter, GitHub AI, or LM Studio).
- **Zero-Config Demo (Server A/B):** Built-in model pools rotate through
  configured remote endpoints. They need no key in this plugin, but depend on
  those third-party services being available and may be rate-limited.
- **NVIDIA Free (API):** Enter an NVIDIA API key in the provider configuration.
  The plugin stores it in Calibre preferences and sends it as a Bearer
  credential to the configured API base URL.
- **NVIDIA Free:** Uses the separately configured proxy route. The source's
  default proxy URL is a placeholder (`https://nvidia-proxy.no-op`); configure
  a working compatible proxy before using this provider.

Provider names, model IDs, endpoints, and example requests are documented in
[`models-report.md`](./models-report.md) and
[`model-calls-report.md`](./model-calls-report.md). The Postman collection is
[`model-calls.postman_collection.json`](./model-calls.postman_collection.json).

## Development and headless debugging

The plugin imports Calibre's Python modules (`calibre`, `calibre.ai`, and Qt),
so run it using Calibre's bundled runtime rather than ordinary system Python.
No `.venv` is required for these checks.

With Calibre installed and available on `PATH`, run a headless provider check
from the repository root:

```powershell
calibre-debug -e debug_test.py -- --test=zero-config
```

On Windows, if `calibre-debug` is not on `PATH`, use its full path:

```powershell
& "C:\Program Files\Calibre2\calibre-debug.exe" -e debug_test.py -- --test=zero-config
```

`debug_test.py` also accepts `--test=classify`, `flash`, `full`,
`nvidia-config`, `nvidia-fetch`, `parse-epub`, or `all`. The classification,
flash, and full-summary tests contact the selected demo AI service; model-fetch
tests make network requests. `parse-epub` requires `--epub <path>`.

For VS Code debugging, use **Attach to Chapter Summarizer (headless flash)** in
Run and Debug and press **F5**. The VS Code task starts `calibre-debug` and the
script waits for the debugger to attach. This avoids launching Calibre's
command-line wrapper as if it were a regular Python interpreter.

To enable additional plugin debug output, set
`CHAPTER_SUMMARIZER_DEBUG_ENABLED=1`. The VS Code task sets it automatically.

## Project layout

| File | Responsibility |
|---|---|
| `__init__.py`, `ui.py` | Calibre plugin registration and interface-action entry point |
| `main.py` | Main/configuration dialogs, provider selection, jobs, and output handling |
| `summarizer.py` | Classification, prompts, AI request/retry handling, and rendering |
| `book_parser.py` | Extract document text and chapter/section structure |
| `cache.py` | Chapter-summary cache and run-history storage |
| `nvidia_free.py`, `nvidia_models_dialog.py` | NVIDIA providers and model browser |
| `zero_config_demo.py` | Built-in demo providers and model rotation |
| `debug_test.py` | Headless development/test entry point |
| `.vscode/` | VS Code attach configuration and headless debug task |

## License and attribution

The plugin source declares **GNU GPL v3**. This branch is for educational use
and retains the original plugin identity and attribution in its source. Consult
the upstream plugin thread and the license terms before redistributing this
branch or a package built from it.
