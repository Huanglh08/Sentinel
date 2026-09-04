import * as React from "react";
import { Brain, Check, ChevronDown, Copy, Pencil, Sparkles } from "lucide-react";
import { toast } from "sonner";

import { FeedbackButtons } from "@/components/chat/FeedbackButtons";
import { MarkdownRenderer } from "@/components/chat/MarkdownRenderer";
import { RecommendedQuestions } from "@/components/chat/RecommendedQuestions";
import { RecommendedQuestionsButton } from "@/components/chat/RecommendedQuestionsButton";
import { SourcesButton } from "@/components/chat/SourcesButton";
import { ThinkingIndicator } from "@/components/chat/ThinkingIndicator";
import { cn } from "@/lib/utils";
import { useChatStore } from "@/stores/chatStore";
import type { Message } from "@/types";

const ASSISTANT_NAME = "Sentinel AI";
const EDIT_TEXTAREA_MAX_HEIGHT = 320;

interface MessageItemProps {
  message: Message;
}

export const MessageItem = React.memo(function MessageItem({ message }: MessageItemProps) {
  const isUser = message.role === "user";
  const showFeedback =
    message.role === "assistant" &&
    message.status !== "streaming" &&
    message.id &&
    !message.id.startsWith("assistant-");
  const isThinking = Boolean(message.isThinking);
  const hasSources =
    message.role === "assistant" &&
    message.status !== "streaming" &&
    (message.sources?.length ?? 0) > 0;
  // 推荐追问：完成态且已落库的助手消息（真实 messageId）方可触发 与反馈按钮判据一致
  const canRecommend =
    message.role === "assistant" &&
    message.status !== "streaming" &&
    Boolean(message.id) &&
    (message.messageStatus ?? "NORMAL") === "NORMAL" &&
    !message.id.startsWith("assistant-");
  const [thinkingExpanded, setThinkingExpanded] = React.useState(false);
  const hasThinking = Boolean(message.thinking && message.thinking.trim().length > 0);
  const hasContent = message.content.trim().length > 0;
  const isWaiting = message.status === "streaming" && !isThinking && !hasContent;

  const editUserMessage = useChatStore((state) => state.editUserMessage);
  const isStreaming = useChatStore((state) => state.isStreaming);
  const [isEditing, setIsEditing] = React.useState(false);
  const [draft, setDraft] = React.useState(message.content);
  const editRef = React.useRef<HTMLTextAreaElement | null>(null);

  const resizeEdit = React.useCallback(() => {
    const el = editRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, EDIT_TEXTAREA_MAX_HEIGHT)}px`;
  }, []);

  React.useEffect(() => {
    if (!isEditing) return;
    resizeEdit();
    const el = editRef.current;
    if (!el) return;
    el.focus({ preventScroll: true });
    el.setSelectionRange(el.value.length, el.value.length);
  }, [isEditing, resizeEdit]);

  const startEdit = React.useCallback(() => {
    if (isStreaming) return;
    setDraft(message.content);
    setIsEditing(true);
  }, [isStreaming, message.content]);

  const cancelEdit = React.useCallback(() => {
    setDraft(message.content);
    setIsEditing(false);
  }, [message.content]);

  // 保存后立刻退出编辑态 由 store 截断该条之后的消息并重新发送 避免按钮停在 loading 观感
  const saveEdit = React.useCallback(() => {
    const trimmed = draft.trim();
    if (!trimmed || isStreaming) return;
    setIsEditing(false);
    if (trimmed === message.content.trim()) return;
    void editUserMessage(message.id, trimmed);
  }, [draft, isStreaming, message.content, message.id, editUserMessage]);

  const handleCopy = React.useCallback(async () => {
    try {
      await navigator.clipboard.writeText(message.content);
      toast.success("复制成功");
    } catch {
      toast.error("复制失败");
    }
  }, [message.content]);

  if (isUser) {
    return (
      <div className="group flex flex-col items-end">
        {isEditing ? (
          <div className="w-full max-w-[80%] rounded-2xl border border-cyan-400/30 bg-slate-900/90 p-3 shadow-[0_8px_24px_rgba(0,0,0,0.25)]">
            <textarea
              ref={editRef}
              value={draft}
              onChange={(event) => {
                setDraft(event.target.value);
                resizeEdit();
              }}
              onKeyDown={(event) => {
                if (event.key === "Escape") {
                  event.preventDefault();
                  cancelEdit();
                  return;
                }
                if (event.key === "Enter" && !event.shiftKey) {
                  const nativeEvent = event.nativeEvent as KeyboardEvent;
                  // 输入法组合态下 Enter 用于选词 不能当作保存
                  if (nativeEvent.isComposing || nativeEvent.keyCode === 229) return;
                  event.preventDefault();
                  saveEdit();
                }
              }}
              rows={1}
              aria-label="编辑消息"
              className="w-full resize-none border-0 bg-transparent text-[15px] leading-relaxed text-slate-100 outline-none"
            />
            <div className="mt-2 flex items-center justify-end gap-2">
              <button
                type="button"
                onClick={cancelEdit}
                className="rounded-lg px-3 py-1.5 text-xs font-medium text-slate-400 transition-colors hover:bg-white/10"
              >
                取消
              </button>
              <button
                type="button"
                onClick={saveEdit}
                disabled={!draft.trim() || isStreaming}
                className={cn(
                  "inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium text-white transition-colors",
                  !draft.trim() || isStreaming
                    ? "cursor-not-allowed bg-white/10 text-slate-500"
                    : "bg-gradient-to-r from-cyan-400 via-blue-500 to-violet-600 hover:brightness-110"
                )}
              >
                <Check className="h-3.5 w-3.5" />
                保存并重发
              </button>
            </div>
          </div>
        ) : (
          <>
            <div className="user-message">
              <p className="whitespace-pre-wrap break-words">{message.content}</p>
            </div>
            <div className="mt-1.5 flex items-center gap-0.5 opacity-0 transition-opacity duration-150 pointer-events-none focus-within:pointer-events-auto focus-within:opacity-100 group-hover:pointer-events-auto group-hover:opacity-100">
              <button
                type="button"
                onClick={handleCopy}
                aria-label="复制"
                title="复制"
                className="flex h-7 w-7 items-center justify-center rounded-md text-slate-500 transition-colors hover:bg-white/10 hover:text-slate-200"
              >
                <Copy className="h-4 w-4" />
              </button>
              <button
                type="button"
                onClick={startEdit}
                disabled={isStreaming}
                aria-label="编辑"
                title={isStreaming ? "生成中无法编辑" : "编辑"}
                className={cn(
                  "flex h-7 w-7 items-center justify-center rounded-md transition-colors",
                  isStreaming
                    ? "cursor-not-allowed text-slate-700"
                    : "text-slate-500 hover:bg-white/10 hover:text-slate-200"
                )}
              >
                <Pencil className="h-4 w-4" />
              </button>
            </div>
          </>
        )}
      </div>
    );
  }

  const thinkingDuration = message.thinkingDuration ? `${message.thinkingDuration}秒` : "";
  return (
    <div className="group flex gap-3">
      <div className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-[#60A5FA] to-[#2563EB] text-white shadow-sm">
        <Sparkles className="h-4 w-4" />
      </div>
      <div className="min-w-0 flex-1 space-y-4">
        <div className="flex items-center gap-2">
          <span className="text-sm font-semibold text-slate-100">{ASSISTANT_NAME}</span>
          {message.isDeepThinking ? (
            <span className="inline-flex items-center gap-1 rounded-full border border-cyan-400/30 bg-cyan-400/10 px-2 py-0.5 text-[11px] font-medium text-cyan-300">
              <Brain className="h-3 w-3" />
              深度思考
            </span>
          ) : null}
        </div>
        {isThinking ? (
          <ThinkingIndicator content={message.thinking} duration={message.thinkingDuration} />
        ) : null}
        {!isThinking && hasThinking ? (
          <div className="overflow-hidden rounded-lg border border-[#BFDBFE] bg-[#DBEAFE]">
            <button
              type="button"
              onClick={() => setThinkingExpanded((prev) => !prev)}
              className="flex w-full items-center gap-2 px-4 py-3 text-left transition-colors hover:bg-[#BFDBFE]/30"
            >
              <div className="flex flex-1 items-center gap-2">
                <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-[#BFDBFE]">
                  <Brain className="h-4 w-4 text-[#2563EB]" />
                </div>
                <span className="text-sm font-medium text-[#2563EB]">深度思考</span>
                {thinkingDuration ? (
                  <span className="rounded-full bg-[#BFDBFE] px-2 py-0.5 text-xs text-[#2563EB]">
                    {thinkingDuration}
                  </span>
                ) : null}
              </div>
              <ChevronDown
                className={cn(
                  "h-4 w-4 text-[#3B82F6] transition-transform",
                  thinkingExpanded && "rotate-180"
                )}
              />
            </button>
            {thinkingExpanded ? (
              <div className="border-t border-[#BFDBFE] px-4 pb-4">
                <div className="mt-3 whitespace-pre-wrap text-sm leading-relaxed text-[#1E40AF]">
                  {message.thinking}
                </div>
              </div>
            ) : null}
          </div>
        ) : null}
        <div className="space-y-2">
          {isWaiting ? (
            <div className="ai-wait" aria-label="思考中">
              <span className="ai-wait-dots" aria-hidden="true">
                <span className="ai-wait-dot" />
                <span className="ai-wait-dot" />
                <span className="ai-wait-dot" />
              </span>
            </div>
          ) : null}
          {hasContent ? (
            <MarkdownRenderer
              content={message.content}
              messageId={message.id}
              sources={message.sources}
            />
          ) : null}
          {message.status === "error" ? (
            <p className="text-xs text-rose-500">生成已中断。</p>
          ) : null}
          {showFeedback || hasSources || canRecommend ? (
            <div className="flex items-center gap-2">
              {showFeedback ? (
                <FeedbackButtons
                  messageId={message.id}
                  feedback={message.feedback ?? null}
                  content={message.content}
                  alwaysVisible
                />
              ) : null}
              {hasSources ? (
                <SourcesButton messageId={message.id} sources={message.sources!} />
              ) : null}
              {canRecommend ? <RecommendedQuestionsButton message={message} /> : null}
            </div>
          ) : null}
          {canRecommend ? <RecommendedQuestions message={message} /> : null}
        </div>
      </div>
    </div>
  );
});
