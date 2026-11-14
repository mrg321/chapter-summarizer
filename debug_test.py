#!/usr/bin/env python
"""
Headless debug entry point for Chapter Summarizer.
Run with: calibre-debug --exec-python=debug_test.py
Or in VS Code: set python interpreter to calibre's python and run this file.
"""

import os
import sys

# Enable debug logging before any imports
os.environ['CHAPTER_SUMMARIZER_DEBUG_ENABLED'] = '1'

if os.environ.get('CHAPTER_SUMMARIZER_WAIT_FOR_DEBUGGER'):
    from pathlib import Path

    extension_root = Path.home() / '.vscode' / 'extensions'
    debugpy_libs = sorted(extension_root.glob('ms-python.debugpy-*/bundled/libs'))
    if not debugpy_libs:
        raise RuntimeError(f'Could not find VS Code debugpy libraries under {extension_root}')
    sys.path.insert(0, str(debugpy_libs[-1]))
    import debugpy

    print('DEBUGPY_STARTING: starting attach listener', flush=True)
    debugpy.listen(('127.0.0.1', 5678), in_process_debug_adapter=True)
    print('DEBUGPY_READY: attach VS Code debugger on 127.0.0.1:5678', flush=True)
    debugpy.wait_for_client()

# Ensure calibre is on sys.path (calibre-debug does this automatically)
# If running via VS Code with calibre's python, this may not be needed.

from calibre_plugins.chapter_summarizer.summarizer import (
    summarize_document,
    flash_summarize_document,
    classify_document,
    SummaryOptions,
    RunStats,
)
from calibre_plugins.chapter_summarizer.book_parser import (
    ParsedDocument,
    ChapterSection,
    parse_document,
)
from calibre_plugins.chapter_summarizer.nvidia_free import (
    get_available_models_rich,
    get_api_key,
    get_api_model,
    get_api_base_url,
)
from calibre_plugins.chapter_summarizer.zero_config_demo import (
    is_zero_config_demo_provider,
    managed_models_for_provider,
    api_url_for_provider,
    planned_model_for_provider,
    ZeroConfigDemoServerAProvider,
    ZeroConfigDemoServerBProvider,
)


def make_test_document() -> ParsedDocument:
    """Create a minimal ParsedDocument for testing without file I/O."""
    raw_text = (
        "Chapter 1\n\n"
        "It was the best of times, it was the worst of times. "
        "The story begins in London and Paris during the French Revolution. "
        "Characters are introduced and the stage is set.\n\n"
        "Chapter 2\n\n"
        "The plot thickens as secrets are revealed. "
        "Sydney Carton makes his famous sacrifice. "
        "The revolution reaches its climax.\n\n"
        "Chapter 3\n\n"
        "Resolution and aftermath. The surviving characters find peace. "
        "A tale of two cities concludes with hope."
    )
    return ParsedDocument(
        title="Test Book",
        source_path="test.epub",
        source_format="epub",
        raw_text=raw_text,
        chapters=[
            ChapterSection(
                index=1,
                title="Chapter 1",
                text=(
                    "It was the best of times, it was the worst of times. "
                    "The story begins in London and Paris during the French Revolution. "
                    "Characters are introduced and the stage is set."
                ),
                identifier="ch1",
            ),
            ChapterSection(
                index=2,
                title="Chapter 2",
                text=(
                    "The plot thickens as secrets are revealed. "
                    "Sydney Carton makes his famous sacrifice. "
                    "The revolution reaches its climax."
                ),
                identifier="ch2",
            ),
            ChapterSection(
                index=3,
                title="Chapter 3",
                text=(
                    "Resolution and aftermath. The surviving characters find peace. "
                    "A tale of two cities concludes with hope."
                ),
                identifier="ch3",
            ),
        ],
    )


def test_zero_config_provider():
    """Test the zero-config demo providers (no API key needed)."""
    print("\n=== Testing Zero-Config Demo Providers ===")
    
    for provider_name in [
        "Zero-Config Demo (Server A)",
        "Zero-Config Demo (Server B)",
    ]:
        print(f"\n--- {provider_name} ---")
        print(f"  is_zero_config_demo_provider: {is_zero_config_demo_provider(provider_name)}")
        print(f"  managed_models: {managed_models_for_provider(provider_name)}")
        print(f"  api_url: {api_url_for_provider(provider_name)}")
        print(f"  planned_model: {planned_model_for_provider(provider_name)}")
        
        # Instantiate the provider directly
        if "Server A" in provider_name:
            provider = ZeroConfigDemoServerAProvider()
        else:
            provider = ZeroConfigDemoServerBProvider()
        
        print(f"  provider.name: {provider.name}")
        print(f"  provider.is_ready_for_use: {provider.is_ready_for_use}")
        print(f"  provider.get_available_models(): {provider.get_available_models()}")


def test_nvidia_free_config():
    """Test NVIDIA Free configuration (no network call)."""
    print("\n=== Testing NVIDIA Free Config ===")
    print(f"  get_api_key(): '{get_api_key()}'")
    print(f"  get_api_model(): '{get_api_model()}'")
    print(f"  get_api_base_url(): '{get_api_base_url()}'")


def test_classify_document():
    """Test document classification."""
    print("\n=== Testing classify_document ===")
    doc = make_test_document()
    opts = SummaryOptions(
        provider_name="Zero-Config Demo (Server A)",
        model_name="",
        max_chunk_words=1400,
        include_highlight=True,
        target_language="same-as-source",
        verbose_ai_log=True,
    )
    
    # Need a provider instance
    from calibre_plugins.chapter_summarizer.summarizer import resolve_provider
    plugin = resolve_provider(opts.provider_name)
    print(f"  Resolved provider: {plugin.name}")
    
    classification = classify_document(plugin, doc, opts, log=print)
    print(f"  Classification: {classification}")


def test_flash_summarize():
    """Test flash summary (single call, no chapter loop)."""
    print("\n=== Testing flash_summarize_document ===")
    doc = make_test_document()
    opts = SummaryOptions(
        provider_name="Zero-Config Demo (Server A)",
        model_name="",
        max_chunk_words=1400,
        include_highlight=True,
        target_language="same-as-source",
        verbose_ai_log=True,
        run_stats=RunStats(started_at=__import__('time').time()),
    )
    
    result = flash_summarize_document(doc, opts, log=print)
    print(f"\n--- Flash Summary Result ---")
    print(f"  document_type: {result.document_type}")
    print(f"  highlight_label: {result.highlight_label}")
    print(f"  model_used: {result.model_used}")
    print(f"  provider_used: {result.provider_used}")
    print(f"  text:\n{result.text}")


def test_full_summarize():
    """Test full chapter-by-chapter summarization."""
    print("\n=== Testing summarize_document (full) ===")
    doc = make_test_document()
    opts = SummaryOptions(
        provider_name="Zero-Config Demo (Server A)",
        model_name="",
        max_chunk_words=1400,
        include_highlight=True,
        target_language="same-as-source",
        verbose_ai_log=True,
        run_stats=RunStats(started_at=__import__('time').time()),
    )
    
    result = summarize_document(doc, opts, log=print)
    print(f"\n--- Full Summary Result ---")
    print(f"  document_type: {result.document_type}")
    print(f"  highlight_label: {result.highlight_label}")
    print(f"  model_used: {result.model_used}")
    print(f"  provider_used: {result.provider_used}")
    print(f"  chapters: {len(result.chapter_summaries)}")
    for ch in result.chapter_summaries:
        print(f"    Ch {ch.index}: {ch.title} ({ch.word_count} words)")
        print(f"      summary: {ch.summary[:80]}...")
        if ch.highlight:
            print(f"      highlight: {ch.highlight[:80]}...")
    print(f"  text:\n{result.text}")


def test_nvidia_model_fetch():
    """Test fetching NVIDIA model list (network call)."""
    print("\n=== Testing NVIDIA model fetch (network) ===")
    try:
        models = get_available_models_rich(use_cache=False, allow_network=True)
        print(f"  Fetched {len(models)} models")
        free_models = [m for m in models if m.get('is_free')]
        print(f"  Free models: {len(free_models)}")
        for m in free_models[:5]:
            print(f"    {m['id']} - {m.get('description', '')[:60]}")
    except Exception as e:
        print(f"  Error (expected if no network/proxy): {e}")


def test_parse_real_epub(epub_path: str):
    """Test parsing a real EPUB file."""
    print(f"\n=== Testing parse_document on {epub_path} ===")
    try:
        doc = parse_document(epub_path)
        print(f"  Title: {doc.title}")
        print(f"  Format: {doc.source_format}")
        print(f"  Total words: {doc.total_words}")
        print(f"  Chapters: {len(doc.chapters)}")
        for ch in doc.chapters[:5]:
            print(f"    Ch {ch.index}: {ch.title} ({ch.word_count} words)")
        if len(doc.chapters) > 5:
            print(f"    ... and {len(doc.chapters) - 5} more")
    except Exception as e:
        print(f"  Error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Headless Chapter Summarizer tests")
    parser.add_argument("--test", choices=[
        "zero-config", "nvidia-config", "classify", "flash", "full", 
        "nvidia-fetch", "parse-epub", "all"
    ], default="all", help="Which test to run")
    parser.add_argument("--epub", help="Path to EPUB for parse-epub test")
    
    args = parser.parse_args()
    
    tests = {
        "zero-config": test_zero_config_provider,
        "nvidia-config": test_nvidia_free_config,
        "classify": test_classify_document,
        "flash": test_flash_summarize,
        "full": test_full_summarize,
        "nvidia-fetch": test_nvidia_model_fetch,
    }
    
    if args.test == "all":
        for name, fn in tests.items():
            try:
                fn()
            except Exception as e:
                print(f"\n[ERROR in {name}] {e}")
                import traceback
                traceback.print_exc()
    elif args.test == "parse-epub":
        if not args.epub:
            print("Error: --epub required for parse-epub test")
            sys.exit(1)
        test_parse_real_epub(args.epub)
    else:
        tests[args.test]()
    
    print("\n=== Done ===")