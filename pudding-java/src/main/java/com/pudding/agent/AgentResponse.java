package com.pudding.agent;

public record AgentResponse(
        AgentType agentType,
        String content,
        boolean success,
        double confidence,
        long latencyMs,
        boolean escalate,
        String toolName,
        boolean toolSuccess,
        boolean toolCached,
        boolean toolReranked,
        String toolError
) {
}
