package com.pudding.trace;

import org.springframework.stereotype.Service;

import java.time.Instant;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;

@Service
public class RequestTraceStore {

    private static final int MAX_SIZE = 200;

    private final Deque<RequestToolTrace> traces = new ArrayDeque<>();
    private final Map<String, RequestToolTrace> byId = new HashMap<>();

    public synchronized void record(RequestToolTrace trace) {
        if (trace == null || trace.requestId() == null || trace.requestId().isBlank()) {
            return;
        }
        RequestToolTrace normalized = trace.timestamp() == null || trace.timestamp().isBlank()
                ? new RequestToolTrace(
                        trace.requestId(),
                        Instant.now().toString(),
                        trace.endpoint(),
                        trace.userId(),
                        trace.conversationId(),
                        trace.intent(),
                        trace.intentGroup(),
                        trace.agentType(),
                        trace.primaryAgent(),
                        trace.supportingAgents(),
                        trace.toolsUsed(),
                        trace.toolCalls(),
                        trace.knowledgeUsed(),
                        trace.escalated(),
                        trace.latencyMs()
                )
                : trace;
        traces.addLast(normalized);
        byId.put(normalized.requestId(), normalized);
        while (traces.size() > MAX_SIZE) {
            RequestToolTrace removed = traces.removeFirst();
            if (removed != null && removed.requestId() != null) {
                byId.remove(removed.requestId());
            }
        }
    }

    public synchronized Optional<RequestToolTrace> find(String requestId) {
        return Optional.ofNullable(byId.get(requestId));
    }

    public synchronized List<RequestToolTrace> recent(int limit) {
        if (limit <= 0 || traces.isEmpty()) {
            return List.of();
        }
        int actual = Math.min(limit, traces.size());
        List<RequestToolTrace> items = new ArrayList<>(actual);
        int skipped = traces.size() - actual;
        int i = 0;
        for (RequestToolTrace trace : traces) {
            if (i++ < skipped) {
                continue;
            }
            items.add(trace);
        }
        return items.reversed();
    }

    public synchronized void clear() {
        traces.clear();
        byId.clear();
    }
}
