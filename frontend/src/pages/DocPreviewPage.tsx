import * as React from "react";
import { ArrowLeft, FileText, Loader2 } from "lucide-react";
import { Link, useParams } from "react-router-dom";

import { DocumentPreview } from "@/components/document/DocumentPreview";
import { getDocument } from "@/services/knowledgeService";

type DocMeta = Awaited<ReturnType<typeof getDocument>>;

export function DocPreviewPage() {
  const { docId } = useParams<{ docId: string }>();
  const [doc, setDoc] = React.useState<DocMeta | null>(null);
  const [status, setStatus] = React.useState<"loading" | "done" | "error">("loading");

  React.useEffect(() => {
    if (!docId) {
      setStatus("error");
      return;
    }
    let cancelled = false;
    setStatus("loading");
    getDocument(docId)
      .then((data) => {
        if (cancelled) return;
        setDoc(data);
        setStatus("done");
        document.title = `${data.docName || "文档"} - 来源预览`;
      })
      .catch(() => {
        if (!cancelled) setStatus("error");
      });
    return () => {
      cancelled = true;
    };
  }, [docId]);

  return (
    <div className="flex h-screen flex-col bg-[#04070F] text-slate-100">
      <header className="app-header flex h-14 shrink-0 items-center gap-4 border-b border-white/10 px-6">
        <Link to="/chat" aria-label="返回对话" className="text-slate-400 transition-colors hover:text-cyan-300">
          <ArrowLeft className="h-5 w-5" />
        </Link>
        <FileText className="h-5 w-5 shrink-0 text-cyan-300" />
        <h1 className="truncate text-sm font-medium text-slate-100" title={doc?.docName || ""}>
          {doc?.docName || "文档预览"}
        </h1>
      </header>
      <div className="flex flex-1 flex-col overflow-hidden bg-[#07101F]">
        {status === "loading" ? (
          <div className="flex flex-1 items-center justify-center gap-2 text-sm text-slate-500">
            <Loader2 className="h-4 w-4 animate-spin" />
            正在加载…
          </div>
        ) : status === "error" || !doc || !docId ? (
          <div className="flex flex-1 items-center justify-center text-sm text-slate-500">
            无法加载该文档，可能已被删除。
          </div>
        ) : (
          <DocumentPreview docId={docId} fileType={doc.fileType} docName={doc.docName} />
        )}
      </div>
    </div>
  );
}
