import * as React from "react";
import { X } from "lucide-react";

import { SourceIcon } from "@/components/chat/SourceIcon";
import { cn } from "@/lib/utils";
import { openSource, sourceSite } from "@/lib/source";
import { useChatStore } from "@/stores/chatStore";

/**
 * 参考来源面板：作为 flex 兄弟项从右侧推挤入场（非模态 不压暗主页）
 * 打开状态由 chatStore.openedSourceMessageId 驱动 关闭时保留内容随宽度收起
 */
export function SourcesPanel() {
  const openedSourceMessageId = useChatStore((state) => state.openedSourceMessageId);
  const messages = useChatStore((state) => state.messages);
  const closeSourcesPanel = useChatStore((state) => state.closeSourcesPanel);

  const open = openedSourceMessageId != null;
  // 来源以 messages 为唯一数据源 按打开的消息 ID 派生 不再单独存一份副本
  const sources =
    messages.find((message) => message.id === openedSourceMessageId)?.sources ?? [];

  // 收起动画期间保留上一次内容 避免瞬间清空闪烁
  const lastSourcesRef = React.useRef(sources);
  if (open && sources.length > 0) {
    lastSourcesRef.current = sources;
  }
  const shownSources = open ? sources : lastSourcesRef.current;

  // 面板打开时按 Esc 关闭
  React.useEffect(() => {
    if (!open) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        closeSourcesPanel();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [open, closeSourcesPanel]);

  return (
    <aside
      className={cn(
        "h-full shrink-0 overflow-hidden transition-[width] duration-300 ease-out",
        open ? "w-[380px] border-l border-white/10" : "w-0"
      )}
      aria-hidden={!open}
    >
      <div className="flex h-full w-[380px] flex-col bg-slate-900/95">
        <div className="flex items-center justify-between border-b border-white/10 px-5 py-4">
          <span className="text-[15px] font-semibold text-slate-100">参考来源 ({shownSources.length})</span>
          <button
            type="button"
            onClick={closeSourcesPanel}
            className="rounded-full p-1.5 text-slate-400 transition-colors hover:bg-white/10 hover:text-slate-200"
            aria-label="关闭"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-3 py-3 sidebar-scroll">
          <ul className="space-y-1">
            {shownSources.map((source, idx) => (
              <li key={`${source.docId}-${idx}`}>
                <button
                  type="button"
                  onClick={() => openSource(source)}
                  title={source.docName || "查看来源"}
                  className="w-full rounded-xl p-3 text-left transition-all hover:bg-white/5"
                >
                  <div className="flex items-start gap-2.5">
                    <span className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-md bg-white/10 text-[11px] font-medium text-slate-300">
                      {source.index ?? idx + 1}
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-sm font-medium text-slate-100">
                        {source.docName || "未命名文档"}
                      </div>
                      <div className="mt-1 flex items-center gap-1.5 text-xs text-slate-400">
                        <span className="flex h-3.5 w-3.5 shrink-0 items-center justify-center">
                          <SourceIcon source={source} className="h-3.5 w-3.5" />
                        </span>
                        <span className="truncate">{sourceSite(source)}</span>
                      </div>
                      {source.excerpt ? (
                        <p className="mt-2 line-clamp-3 text-xs leading-relaxed text-slate-400">
                          {source.excerpt}
                        </p>
                      ) : null}
                    </div>
                  </div>
                </button>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </aside>
  );
}
