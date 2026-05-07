<script lang="ts">
  let { name, glyph, pr, hint }: {
    name: string;
    glyph: string;
    pr: string;
    hint?: string;
  } = $props();
</script>

<div class="h-full flex items-center justify-center bg-slate-950">
  <div class="max-w-md text-center px-8">
    <div class="text-slate-700 text-3xl mb-3 font-mono select-none">{glyph}</div>
    <div class="font-mono text-xl text-slate-300 mb-2">/{name}</div>
    <p class="text-sm text-slate-500 leading-relaxed">
      Coming in <span class="font-mono text-review">{pr}</span>{#if hint} — {hint}{/if}.
    </p>
    <p class="text-xs text-slate-600 mt-3">
      For now, switch to <span class="font-mono text-slate-400">/agents</span> via the spine.
    </p>
  </div>
</div>
