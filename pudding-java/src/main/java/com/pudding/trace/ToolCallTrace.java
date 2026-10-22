package com.pudding.trace;

public record ToolCallTrace(
        String toolName,
        boolean success,
        boolean fallbackUsed,
        boolean cached,
        boolean reranked,
        long latencyMs,
        String error
) {
}
