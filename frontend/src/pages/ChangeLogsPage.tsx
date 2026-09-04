import { Link } from "react-router-dom";
import { ArrowLeft, MessageSquare } from "lucide-react";

import { BizChangeLogPage } from "@/pages/admin/change-logs/BizChangeLogPage";

export function ChangeLogsPage() {
  return (
    <div className="change-logs-page min-h-screen bg-[#04070F] text-slate-100">
      <header className="app-header flex h-14 items-center justify-between border-b border-white/10 px-6">
        <div className="flex items-center gap-4">
          <Link to="/chat" aria-label="返回对话" className="text-slate-400 transition-colors hover:text-cyan-300">
            <ArrowLeft className="h-5 w-5" />
          </Link>
          <div>
            <div className="text-sm font-semibold text-slate-100">sentinel</div>
            <div className="text-[11px] uppercase tracking-[0.18em] text-slate-500">业务变更审计</div>
          </div>
        </div>
        <Link to="/chat" className="inline-flex items-center gap-2 text-sm text-slate-400 transition-colors hover:text-cyan-300">
          <MessageSquare className="h-4 w-4" />
          对话
        </Link>
      </header>
      <main className="mx-auto max-w-6xl px-6 py-8">
        <BizChangeLogPage />
      </main>
    </div>
  );
}
