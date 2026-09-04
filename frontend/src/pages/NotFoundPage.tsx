import { ArrowLeft, Radar } from "lucide-react";
import { Link } from "react-router-dom";

export function NotFoundPage() {
  return (
    <div className="relative flex min-h-screen items-center justify-center overflow-hidden bg-[#04070F] px-4 text-slate-100">
      <div aria-hidden="true" className="absolute h-96 w-96 rounded-full bg-cyan-400/10 blur-3xl" />
      <div className="relative w-full max-w-lg text-center">
        <div className="mx-auto flex h-14 w-14 items-center justify-center rounded-2xl border border-cyan-400/25 bg-cyan-400/10 text-cyan-300">
          <Radar className="h-7 w-7" />
        </div>
        <p className="mt-8 font-mono text-8xl font-semibold tracking-[-0.08em] text-gradient">404</p>
        <p className="mt-2 text-xl font-semibold text-slate-100">信号未找到</p>
        <p className="mx-auto mt-3 max-w-sm text-sm leading-6 text-slate-500">你访问的页面不存在，或当前路径已经失效。</p>
        <Link
          to="/chat"
          className="mt-8 inline-flex items-center gap-2 rounded-xl bg-gradient-to-r from-cyan-400 to-blue-500 px-5 py-2.5 text-sm font-semibold text-[#04070F] transition hover:brightness-110"
        >
          <ArrowLeft className="h-4 w-4" />
          返回对话
        </Link>
      </div>
    </div>
  );
}
