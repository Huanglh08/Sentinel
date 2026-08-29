import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle
} from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow
} from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";
import { toast } from "sonner";
import {
  closeCase,
  decideApproval,
  exportCaseReport,
  getCaseDetail,
  listApprovals,
  listCases,
  streamInvestigate,
  type ApprovalItem,
  type CaseDetail,
  type CaseListItem,
  type InvestigationSseEvent
} from "@/services/caseService";
import { Archive, CheckCircle2, FileDown, Gavel, Loader2, Search, XCircle } from "lucide-react";

const STATUS_VARIANT: Record<string, string> = {
  OPEN: "bg-blue-100 text-blue-700",
  INVESTIGATING: "bg-amber-100 text-amber-700",
  REPORTED: "bg-violet-100 text-violet-700",
  CLOSED: "bg-gray-100 text-gray-600"
};

const CONCLUSION_VARIANT: Record<string, string> = {
  malicious: "bg-red-100 text-red-700",
  suspicious: "bg-orange-100 text-orange-700",
  benign: "bg-green-100 text-green-700",
  inconclusive: "bg-gray-100 text-gray-600"
};

const PRIORITY_VARIANT: Record<string, string> = {
  P0: "bg-red-100 text-red-700",
  P1: "bg-orange-100 text-orange-700",
  P2: "bg-yellow-100 text-yellow-700",
  P3: "bg-gray-100 text-gray-600"
};

type StreamLogLine = { text: string; kind: "phase" | "step" | "ok" | "error" };

export function CaseInvestigationPage() {
  const [tab, setTab] = useState<"cases" | "approvals">("cases");
  const [cases, setCases] = useState<CaseListItem[]>([]);
  const [approvals, setApprovals] = useState<ApprovalItem[]>([]);
  const [loading, setLoading] = useState(false);

  // 发起调查
  const [investigateOpen, setInvestigateOpen] = useState(false);
  const [clue, setClue] = useState("");
  const [source, setSource] = useState("MANUAL");
  const [running, setRunning] = useState(false);
  const [logLines, setLogLines] = useState<StreamLogLine[]>([]);
  const abortRef = useRef<AbortController | null>(null);

  // 案件详情
  const [detail, setDetail] = useState<CaseDetail | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);

  // 列表筛选（P3-5：状态 + 优先级，本地过滤）
  const [filterStatus, setFilterStatus] = useState("ALL");
  const [filterPriority, setFilterPriority] = useState("ALL");
  const filteredCases = useMemo(
    () =>
      cases.filter(
        (c) =>
          (filterStatus === "ALL" || c.status === filterStatus) &&
          (filterPriority === "ALL" || c.priority === filterPriority)
      ),
    [cases, filterStatus, filterPriority]
  );

  // 审批操作
  const [decideTarget, setDecideTarget] = useState<ApprovalItem | null>(null);
  const [decideAction, setDecideAction] = useState<"APPROVED" | "REJECTED">("APPROVED");
  const [decideOpinion, setDecideOpinion] = useState("");
  const [deciding, setDeciding] = useState(false);

  const refreshCases = useCallback(async () => {
    setLoading(true);
    try {
      const res = await listCases(50);
      setCases(res.cases || []);
    } catch (e) {
      toast.error("案件列表加载失败", {
        description: e instanceof Error ? e.message : String(e)
      });
    } finally {
      setLoading(false);
    }
  }, []);

  const refreshApprovals = useCallback(async () => {
    setLoading(true);
    try {
      const res = await listApprovals({ limit: 100 });
      setApprovals(res.approvals || []);
    } catch (e) {
      toast.error("审批列表加载失败", {
        description: e instanceof Error ? e.message : String(e)
      });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (tab === "cases") {
      void refreshCases();
    } else {
      void refreshApprovals();
    }
  }, [tab, refreshCases, refreshApprovals]);

  // 归档案件（P3-3：REPORTED/INVESTIGATING -> CLOSED，幂等）
  const handleArchive = async (c: CaseListItem) => {
    try {
      const res = await closeCase(c.id);
      if (res.closed) {
        toast.success(`案件已归档：${c.case_no}`);
      } else {
        toast.info(`案件已处于归档状态：${c.case_no}`);
      }
      void refreshCases();
      if (detail?.id === c.id) {
        setDetail(await getCaseDetail(c.id));
      }
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "归档失败");
    }
  };

  // 导出调查报告 Markdown（P3-3）
  const handleExport = async (c: CaseDetail | CaseListItem) => {
    try {
      await exportCaseReport(c.id, c.case_no);
      toast.success(`报告已导出：${c.case_no}-report.md`);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "导出失败");
    }
  };

  const handleInvestigate = async () => {
    if (clue.trim().length < 5) {
      toast.error("线索过短", { description: "至少 5 个字符" });
      return;
    }
    setRunning(true);
    setLogLines([]);
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      await streamInvestigate(
        clue.trim(),
        source,
        (ev: InvestigationSseEvent) => {
          if (ev.type === "__end__") {
            setLogLines((prev) => [...prev, { text: "■ 调查完成", kind: "ok" }]);
            return;
          }
          if (ev.type === "__error__") {
            setLogLines((prev) => [...prev, { text: `✗ ${ev.message || "调查失败"}`, kind: "error" }]);
            return;
          }
          if (ev.type === "phase") {
            setLogLines((prev) => [...prev, { text: `▶ 阶段: ${ev.phase} ${ev.status || ""}`, kind: "phase" }]);
          } else if (ev.type === "case.created") {
            setLogLines((prev) => [...prev, { text: `● 建案 ${ev.case_no} (${ev.case_id})`, kind: "ok" }]);
          } else if (ev.type === "step") {
            setLogLines((prev) => [
              ...prev,
              {
                text: `${ev.is_error ? "✗" : "·"} step${ev.step_no} ${ev.tool_id}${ev.is_error ? " [失败]" : ""}`,
                kind: ev.is_error ? "error" : "step"
              }
            ]);
          } else if (ev.type === "approval.created") {
            setLogLines((prev) => [...prev, { text: `⚖ 生成 ${ev.approvals?.length || 0} 条人工审批待办`, kind: "ok" }]);
          } else if (ev.type === "report") {
            const r = ev.report as { conclusion?: string; confidence?: number } | undefined;
            setLogLines((prev) => [
              ...prev,
              { text: `■ 报告: ${r?.conclusion || "-"} (置信度 ${r?.confidence ?? "-"})`, kind: "ok" }
            ]);
          }
        },
        controller.signal
      );
      void refreshCases();
    } catch (e) {
      if ((e as Error).name !== "AbortError") {
        setLogLines((prev) => [...prev, { text: `✗ ${e instanceof Error ? e.message : String(e)}`, kind: "error" }]);
      }
    } finally {
      setRunning(false);
      abortRef.current = null;
    }
  };

  const openDetail = async (c: CaseListItem) => {
    try {
      const d = await getCaseDetail(c.id);
      setDetail(d);
      setDetailOpen(true);
    } catch (e) {
      toast.error("案件详情加载失败", {
        description: e instanceof Error ? e.message : String(e)
      });
    }
  };

  const submitDecide = async () => {
    if (!decideTarget) {
      return;
    }
    setDeciding(true);
    try {
      await decideApproval(decideTarget.id, decideAction, decideOpinion);
      toast.success(decideAction === "APPROVED" ? "已批准" : "已驳回");
      setDecideTarget(null);
      setDecideOpinion("");
      void refreshApprovals();
    } catch (e) {
      toast.error("裁决失败", {
        description: e instanceof Error ? e.message : String(e)
      });
    } finally {
      setDeciding(false);
    }
  };

  const report = detail?.report_json as
    | {
        conclusion?: string;
        confidence?: number;
        summary?: string;
        key_findings?: { finding?: string; evidence_step?: number }[];
        recommendations?: { action?: string; requires_human_approval?: boolean }[];
        knowledge_distillation?: string;
      }
    | undefined;

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="flex flex-row items-center justify-between space-y-0">
          <div>
            <CardTitle>案件智能调查</CardTitle>
            <CardDescription>线索 → 分诊 → 受控多步调查 → 结构化报告 → 人工审批闭环</CardDescription>
          </div>
          <div className="flex items-center gap-2">
            <Button
              variant={tab === "cases" ? "default" : "outline"}
              size="sm"
              onClick={() => setTab("cases")}
            >
              案件列表
            </Button>
            <Button
              variant={tab === "approvals" ? "default" : "outline"}
              size="sm"
              onClick={() => setTab("approvals")}
            >
              <Gavel className="mr-1 h-4 w-4" />
              审批待办
            </Button>
            <Button size="sm" onClick={() => { setLogLines([]); setInvestigateOpen(true); }}>
              <Search className="mr-1 h-4 w-4" />
              发起调查
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          {tab === "cases" ? (
            <div className="space-y-3">
              <div className="flex items-center gap-2">
                <select
                  className="h-8 rounded-md border border-input bg-background px-2 text-xs"
                  value={filterStatus}
                  onChange={(e) => setFilterStatus(e.target.value)}
                >
                  <option value="ALL">全部状态</option>
                  <option value="OPEN">OPEN</option>
                  <option value="INVESTIGATING">INVESTIGATING</option>
                  <option value="REPORTED">REPORTED</option>
                  <option value="CLOSED">CLOSED</option>
                </select>
                <select
                  className="h-8 rounded-md border border-input bg-background px-2 text-xs"
                  value={filterPriority}
                  onChange={(e) => setFilterPriority(e.target.value)}
                >
                  <option value="ALL">全部优先级</option>
                  <option value="P0">P0</option>
                  <option value="P1">P1</option>
                  <option value="P2">P2</option>
                  <option value="P3">P3</option>
                </select>
                <span className="text-xs text-muted-foreground">
                  {filteredCases.length}/{cases.length} 条
                </span>
              </div>
              <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>案件号</TableHead>
                  <TableHead>标题</TableHead>
                  <TableHead>状态</TableHead>
                  <TableHead>优先级</TableHead>
                  <TableHead>调查对象</TableHead>
                  <TableHead>创建时间</TableHead>
                  <TableHead className="text-right">操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {loading && cases.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={7} className="h-24 text-center">
                      <Loader2 className="mx-auto h-5 w-5 animate-spin" />
                    </TableCell>
                  </TableRow>
                ) : filteredCases.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={7} className="h-24 text-center text-muted-foreground">
                      暂无案件，点击「发起调查」开始
                    </TableCell>
                  </TableRow>
                ) : (
                  filteredCases.map((c) => (
                    <TableRow key={c.id} className="cursor-pointer" onClick={() => void openDetail(c)}>
                      <TableCell className="font-mono text-xs">{c.case_no}</TableCell>
                      <TableCell className="max-w-[280px] truncate">{c.title}</TableCell>
                      <TableCell>
                        <Badge variant="outline" className={STATUS_VARIANT[c.status] || ""}>
                          {c.status}
                        </Badge>
                      </TableCell>
                      <TableCell>
                        <Badge variant="outline" className={PRIORITY_VARIANT[c.priority] || ""}>
                          {c.priority}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-xs text-muted-foreground">
                        {c.subject_type}/{c.subject_id}
                      </TableCell>
                      <TableCell className="text-xs">{c.create_time || "-"}</TableCell>
                      <TableCell className="text-right">
                        <div className="flex justify-end gap-1">
                          <Button
                            variant="ghost"
                            size="sm"
                            title="导出 Markdown 报告"
                            onClick={(e) => { e.stopPropagation(); void handleExport(c); }}
                          >
                            <FileDown className="h-4 w-4" />
                          </Button>
                          {c.status !== "CLOSED" && (
                            <Button
                              variant="ghost"
                              size="sm"
                              title="归档关闭"
                              onClick={(e) => { e.stopPropagation(); void handleArchive(c); }}
                            >
                              <Archive className="h-4 w-4" />
                            </Button>
                          )}
                          <Button variant="ghost" size="sm" onClick={(e) => { e.stopPropagation(); void openDetail(c); }}>
                            详情
                          </Button>
                        </div>
                      </TableCell>
                    </TableRow>
                  ))
                )}
              </TableBody>
              </Table>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>案件号</TableHead>
                  <TableHead>建议动作</TableHead>
                  <TableHead>理由</TableHead>
                  <TableHead>状态</TableHead>
                  <TableHead>审批人</TableHead>
                  <TableHead className="text-right">操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {loading && approvals.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={6} className="h-24 text-center">
                      <Loader2 className="mx-auto h-5 w-5 animate-spin" />
                    </TableCell>
                  </TableRow>
                ) : approvals.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={6} className="h-24 text-center text-muted-foreground">
                      暂无审批待办
                    </TableCell>
                  </TableRow>
                ) : (
                  approvals.map((a) => (
                    <TableRow key={a.id}>
                      <TableCell className="font-mono text-xs">{a.case_no}</TableCell>
                      <TableCell className="max-w-[200px] truncate font-medium">{a.action}</TableCell>
                      <TableCell className="max-w-[280px] truncate text-muted-foreground">{a.reason}</TableCell>
                      <TableCell>
                        <Badge
                          variant="outline"
                          className={
                            a.status === "APPROVED"
                              ? "bg-green-100 text-green-700"
                              : a.status === "REJECTED"
                                ? "bg-red-100 text-red-700"
                                : "bg-amber-100 text-amber-700"
                          }
                        >
                          {a.status}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-xs">{a.operator || "-"}</TableCell>
                      <TableCell className="text-right">
                        {a.status === "PENDING" ? (
                          <div className="flex justify-end gap-1">
                            <Button
                              size="sm"
                              variant="outline"
                              className="text-green-600"
                              onClick={() => { setDecideTarget(a); setDecideAction("APPROVED"); setDecideOpinion(""); }}
                            >
                              <CheckCircle2 className="mr-1 h-4 w-4" />
                              批准
                            </Button>
                            <Button
                              size="sm"
                              variant="outline"
                              className="text-red-600"
                              onClick={() => { setDecideTarget(a); setDecideAction("REJECTED"); setDecideOpinion(""); }}
                            >
                              <XCircle className="mr-1 h-4 w-4" />
                              驳回
                            </Button>
                          </div>
                        ) : (
                          <span className="text-xs text-muted-foreground">{a.opinion || "-"}</span>
                        )}
                      </TableCell>
                    </TableRow>
                  ))
                )}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {/* 发起调查 */}
      <Dialog open={investigateOpen} onOpenChange={(o) => { if (!running) { setInvestigateOpen(o); } }}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>发起案件调查</DialogTitle>
            <DialogDescription>输入风控线索，系统将自动分诊、多步调查并输出结构化报告</DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <div className="space-y-1">
              <Label htmlFor="clue">案件线索</Label>
              <Textarea
                id="clue"
                rows={5}
                placeholder="例：账号 user_9527 十分钟内从同一 IP 发起 327 次登录尝试，命中 89 个不同密码…"
                value={clue}
                onChange={(e) => setClue(e.target.value)}
                disabled={running}
              />
            </div>
            <div className="space-y-1">
              <Label>线索来源</Label>
              <Select value={source} onValueChange={setSource} disabled={running}>
                <SelectTrigger className="w-48">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="MANUAL">人工录入</SelectItem>
                  <SelectItem value="RULE_ENGINE">规则引擎</SelectItem>
                  <SelectItem value="MONITOR">监控告警</SelectItem>
                </SelectContent>
              </Select>
            </div>
            {logLines.length > 0 && (
              <div className="max-h-64 overflow-auto rounded-md bg-gray-950 p-3 font-mono text-xs leading-5">
                {logLines.map((l, i) => (
                  <div
                    key={i}
                    className={
                      l.kind === "error"
                        ? "text-red-400"
                        : l.kind === "ok"
                          ? "text-green-400"
                          : l.kind === "phase"
                            ? "text-sky-400"
                            : "text-gray-300"
                    }
                  >
                    {l.text}
                  </div>
                ))}
                {running && <Loader2 className="mt-1 h-3 w-3 animate-spin text-gray-400" />}
              </div>
            )}
          </div>
          <DialogFooter>
            {running ? (
              <Button variant="secondary" onClick={() => abortRef.current?.abort()}>
                中断
              </Button>
            ) : (
              <>
                <Button variant="outline" onClick={() => setInvestigateOpen(false)}>
                  关闭
                </Button>
                <Button onClick={() => void handleInvestigate()} disabled={clue.trim().length < 5}>
                  开始调查
                </Button>
              </>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* 案件详情 */}
      <Dialog open={detailOpen} onOpenChange={setDetailOpen}>
        <DialogContent className="max-w-3xl max-h-[85vh] overflow-auto">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              {detail?.case_no}
              {report?.conclusion && (
                <Badge variant="outline" className={CONCLUSION_VARIANT[report.conclusion] || ""}>
                  {report.conclusion}
                  {typeof report.confidence === "number" ? ` · ${report.confidence}` : ""}
                </Badge>
              )}
            </DialogTitle>
            <DialogDescription>{detail?.title}</DialogDescription>
          </DialogHeader>
          {detail && (
            <div className="space-y-4 text-sm">
              <div className="grid grid-cols-2 gap-2 text-xs text-muted-foreground">
                <div>调查对象: {detail.subject_type}/{detail.subject_id}</div>
                <div>来源: {detail.source || "-"}</div>
                <div>TTA: {detail.tta_ms != null ? `${detail.tta_ms}ms` : "-"}</div>
                <div>TTR: {detail.ttr_ms != null ? `${detail.ttr_ms}ms` : "-"}</div>
              </div>
              {detail.clue && (
                <div>
                  <div className="mb-1 font-medium">线索</div>
                  <div className="rounded-md bg-gray-50 p-2 text-xs whitespace-pre-wrap">{detail.clue}</div>
                </div>
              )}
              {report?.summary && (
                <div>
                  <div className="mb-1 font-medium">结论摘要</div>
                  <div className="rounded-md bg-gray-50 p-2 text-xs whitespace-pre-wrap">{report.summary}</div>
                </div>
              )}
              {!!report?.key_findings?.length && (
                <div>
                  <div className="mb-1 font-medium">关键发现</div>
                  <ul className="list-disc space-y-1 pl-5 text-xs">
                    {report.key_findings.map((f, i) => (
                      <li key={i}>{f.finding}{f.evidence_step != null ? `（证据 step${f.evidence_step}）` : ""}</li>
                    ))}
                  </ul>
                </div>
              )}
              {!!report?.recommendations?.length && (
                <div>
                  <div className="mb-1 font-medium">处置建议</div>
                  <ul className="list-disc space-y-1 pl-5 text-xs">
                    {report.recommendations.map((r, i) => (
                      <li key={i}>
                        {r.action}
                        {r.requires_human_approval && (
                          <Badge variant="outline" className="ml-1 bg-amber-100 text-amber-700">需人工审批</Badge>
                        )}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {!!detail.evidence?.length && (
                <div>
                  <div className="mb-1 font-medium">证据链（{detail.evidence.length} 步）</div>
                  {/* P3-5：时间线样式（左侧竖线 + 节点圆点） */}
                  <div className="relative space-y-3 pl-5 before:absolute before:bottom-1 before:left-[7px] before:top-1 before:w-px before:bg-border">
                    {detail.evidence.map((s) => (
                      <div key={s.step_no} className="relative text-xs">
                        <span
                          className={`absolute -left-[18px] top-1 h-2.5 w-2.5 rounded-full border-2 border-background ${
                            s.is_error ? "bg-red-500" : "bg-primary"
                          }`}
                        />
                        <div className="rounded-md border p-2">
                          <div className="flex items-center gap-2">
                            <Badge variant="outline" className="font-mono">step{s.step_no}</Badge>
                            <span className="font-medium">{s.tool_id}</span>
                            {s.is_error && <Badge variant="outline" className="bg-red-100 text-red-700">失败</Badge>}
                            {s.elapsed_ms != null && <span className="text-muted-foreground">{s.elapsed_ms}ms</span>}
                          </div>
                          {s.thought && <div className="mt-1 text-muted-foreground">{s.thought}</div>}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* P3-1：处置记录 */}
              {!!detail.actions?.length && (
                <div>
                  <div className="mb-1 font-medium">处置记录（{detail.actions.length} 条）</div>
                  <div className="space-y-1">
                    {detail.actions.map((a) => (
                      <div key={a.id} className="rounded-md border p-2 text-xs">
                        <div className="flex items-center gap-2">
                          <Badge variant="outline" className="bg-emerald-100 text-emerald-700">{a.status}</Badge>
                          <span className="font-medium">{a.action}</span>
                          <span className="ml-auto text-muted-foreground">
                            {a.operator} · {a.create_time}
                          </span>
                        </div>
                        {a.detail && <div className="mt-1 text-muted-foreground">{a.detail}</div>}
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* P3-2：审计日志 */}
              {!!detail.audits?.length && (
                <div>
                  <div className="mb-1 font-medium">审计日志（{detail.audits.length} 条）</div>
                  <div className="space-y-1">
                    {detail.audits.map((a) => (
                      <div key={a.id} className="flex items-center gap-2 rounded-md border p-2 text-xs">
                        <Badge variant="outline" className="font-mono">{a.operation}</Badge>
                        <span className="text-muted-foreground">{a.detail}</span>
                        <span className="ml-auto shrink-0 text-muted-foreground">
                          {a.operator} · {a.create_time}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}
          {/* P3-3：详情弹窗操作 */}
          <DialogFooter>
            {detail && (
              <div className="flex gap-2">
                <Button variant="outline" size="sm" onClick={() => void handleExport(detail)}>
                  <FileDown className="mr-1 h-4 w-4" />
                  导出报告
                </Button>
                {detail.status !== "CLOSED" && (
                  <Button variant="outline" size="sm" onClick={() => void handleArchive(detail)}>
                    <Archive className="mr-1 h-4 w-4" />
                    归档
                  </Button>
                )}
              </div>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* 审批裁决 */}
      <Dialog open={!!decideTarget} onOpenChange={(o) => { if (!o) { setDecideTarget(null); } }}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>{decideAction === "APPROVED" ? "批准处置" : "驳回建议"}</DialogTitle>
            <DialogDescription>
              {decideTarget?.action} — {decideTarget?.case_no}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2">
            {decideTarget?.reason && (
              <div className="rounded-md bg-gray-50 p-2 text-xs whitespace-pre-wrap">{decideTarget.reason}</div>
            )}
            <div className="space-y-1">
              <Label htmlFor="opinion">审批意见</Label>
              <Input
                id="opinion"
                value={decideOpinion}
                onChange={(e) => setDecideOpinion(e.target.value)}
                placeholder="选填"
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDecideTarget(null)} disabled={deciding}>
              取消
            </Button>
            <Button onClick={() => void submitDecide()} disabled={deciding}>
              {deciding && <Loader2 className="mr-1 h-4 w-4 animate-spin" />}
              确认{decideAction === "APPROVED" ? "批准" : "驳回"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

export default CaseInvestigationPage;
