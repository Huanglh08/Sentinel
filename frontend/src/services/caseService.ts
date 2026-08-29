import { api } from "@/services/api";
import { storage } from "@/utils/storage";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "";

// ---------------------------------------------------------------------------
// 类型（与 agent-python /case/* 接口对齐）
// ---------------------------------------------------------------------------
export type CaseListItem = {
  id: string;
  case_no: string;
  title: string;
  status: string;
  priority: string;
  severity?: string;
  subject_type?: string;
  subject_id?: string;
  create_time?: string;
};

export type CaseEvidenceStep = {
  step_no: number;
  phase: string;
  tool_id?: string;
  arguments?: Record<string, unknown>;
  observation?: string;
  thought?: string;
  is_error?: boolean;
  elapsed_ms?: number;
  create_time?: string;
};

export type CaseDetail = CaseListItem & {
  user_id?: string;
  source?: string;
  clue?: string;
  triage_json?: Record<string, unknown>;
  report_json?: Record<string, unknown>;
  confidence?: number;
  conclusion?: string;
  tta_ms?: number;
  ttr_ms?: number;
  evidence?: CaseEvidenceStep[];
};

export type ApprovalItem = {
  id: string;
  case_id: string;
  case_no: string;
  action: string;
  reason: string;
  status: "PENDING" | "APPROVED" | "REJECTED";
  operator: string;
  opinion: string;
  create_time: string;
  update_time: string;
};

export type InvestigationSseEvent = {
  type: string;
  phase?: string;
  status?: string;
  case_id?: string;
  case_no?: string;
  step_no?: number;
  tool_id?: string;
  thought?: string;
  observation?: string;
  is_error?: boolean;
  report?: Record<string, unknown>;
  approvals?: ApprovalItem[];
  message?: string;
};

// ---------------------------------------------------------------------------
// REST 接口
// ---------------------------------------------------------------------------
export async function listCases(limit = 50): Promise<{ cases: CaseListItem[] }> {
  return api.get<{ cases: CaseListItem[] }, { cases: CaseListItem[] }>("/cases", {
    params: { limit }
  });
}

export async function getCaseDetail(caseId: string): Promise<CaseDetail> {
  return api.get<CaseDetail, CaseDetail>(`/case/${caseId}`);
}

export async function listApprovals(params?: {
  case_id?: string;
  status?: string;
  limit?: number;
}): Promise<{ approvals: ApprovalItem[] }> {
  return api.get<{ approvals: ApprovalItem[] }, { approvals: ApprovalItem[] }>(
    "/case/approvals",
    { params }
  );
}

export async function decideApproval(
  approvalId: string,
  decision: "APPROVED" | "REJECTED",
  opinion = ""
): Promise<ApprovalItem> {
  return api.post<ApprovalItem, ApprovalItem>(
    `/case/approvals/${approvalId}/decide`,
    { decision, opinion }
  );
}

// ---------------------------------------------------------------------------
// 发起调查：POST SSE 流式解析（EventSource 不支持 POST，用 fetch + reader）
// ---------------------------------------------------------------------------
export async function streamInvestigate(
  clue: string,
  source: string,
  onEvent: (event: InvestigationSseEvent) => void,
  signal?: AbortSignal
): Promise<void> {
  const resp = await fetch(`${API_BASE_URL}/case/investigate`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: storage.getToken() || ""
    },
    body: JSON.stringify({ clue, source }),
    signal
  });
  if (!resp.ok || !resp.body) {
    throw new Error(`调查请求失败: HTTP ${resp.status}`);
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  const dispatch = (raw: string) => {
    let eventName = "message";
    const dataLines: string[] = [];
    for (const line of raw.split("\n")) {
      if (line.startsWith("event:")) {
        eventName = line.slice(6).trim();
      } else if (line.startsWith("data:")) {
        dataLines.push(line.slice(5).trim());
      }
    }
    if (dataLines.length === 0) {
      return;
    }
    try {
      const payload = JSON.parse(dataLines.join("\n"));
      if (eventName === "end") {
        onEvent({ type: "__end__" });
      } else if (eventName === "error") {
        onEvent({ type: "__error__", message: payload?.message || "调查失败" });
      } else {
        onEvent(payload as InvestigationSseEvent);
      }
    } catch {
      // 忽略无法解析的分片
    }
  };

  for (;;) {
    const { done, value } = await reader.read();
    if (done) {
      break;
    }
    buffer += decoder.decode(value, { stream: true });
    let sepIndex = buffer.indexOf("\n\n");
    while (sepIndex >= 0) {
      const raw = buffer.slice(0, sepIndex);
      buffer = buffer.slice(sepIndex + 2);
      dispatch(raw);
      sepIndex = buffer.indexOf("\n\n");
    }
  }
  if (buffer.trim()) {
    dispatch(buffer.trim());
  }
}
