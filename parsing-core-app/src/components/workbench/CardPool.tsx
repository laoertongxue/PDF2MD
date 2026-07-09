import { Link } from "react-router-dom";

export default function CardPool() {
  return (
    <div className="space-y-5 animate-in">
      <div>
        <h1 className="text-xl font-semibold text-zinc-900">课程卡片池</h1>
        <p className="mt-1 text-sm text-zinc-500">章节精读沉淀的卡片会汇总到这里。</p>
      </div>

      <div className="rounded-lg border border-dashed border-zinc-300 bg-white px-8 py-12 text-center">
        <p className="text-sm font-medium text-zinc-700">暂无卡片</p>
        <p className="mt-1 text-xs text-zinc-400">当前前端 store 尚未提供 cards 数据读取，先保留空状态。</p>
        <Link to="/workbench/chapter" className="mt-4 inline-flex rounded-md bg-zinc-900 px-4 py-2 text-sm font-medium text-white">
          返回章节精读
        </Link>
      </div>
    </div>
  );
}
