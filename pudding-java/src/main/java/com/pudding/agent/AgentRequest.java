package com.pudding.agent;

import com.pudding.intent.IntentCategory;
import com.pudding.intent.IntentResult;
import com.pudding.intent.UrgencyLevel;

import java.util.List;
import java.util.Map;
import java.util.UUID;

public record AgentRequest(
        String message,
        String userId,
        String conversationId,
        String context,
        List<Map<String, String>> history,
        Map<String, List<String>> entities,
        IntentCategory intent,
        String intentGroup,
        UrgencyLevel urgency,
        double intentConfidence,
        String requestId
) {
    public AgentRequest withIntent(IntentResult intentResult) {
        return new AgentRequest(
                message,
                userId,
                conversationId,
                context,
                history,
                intentResult.entities(),
                intentResult.intent(),
                intentResult.intentGroup(),
                intentResult.urgency(),
                intentResult.confidence(),
                requestId
        );
    }

    public static AgentRequest of(String message, String userId, String conversationId, String context, List<Map<String, String>> history) {
        return of(message, userId, conversationId, context, history, null, newRequestId());
    }

    public static AgentRequest of(
            String message,
            String userId,
            String conversationId,
            String context,
            List<Map<String, String>> history,
            IntentResult intentResult
    ) {
        return of(message, userId, conversationId, context, history, intentResult, newRequestId());
    }

    public static AgentRequest of(
            String message,
            String userId,
            String conversationId,
            String context,
            List<Map<String, String>> history,
            IntentResult intentResult,
            String requestId
    ) {
        return new AgentRequest(
                message,
                userId,
                conversationId,
                context,
                history,
                intentResult == null ? Map.of() : intentResult.entities(),
                intentResult == null ? null : intentResult.intent(),
                intentResult == null ? null : intentResult.intentGroup(),
                intentResult == null ? null : intentResult.urgency(),
                intentResult == null ? 1.0 : intentResult.confidence(),
                requestId == null || requestId.isBlank() ? newRequestId() : requestId
        );
    }

    private static String newRequestId() {
        return UUID.randomUUID().toString().substring(0, 8);
    }
}
