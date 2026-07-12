import { useEffect, useMemo, useRef, useState } from "react";
import { Pencil, Search, Star, X } from "lucide-react";
import { Link, useSearchParams } from "react-router-dom";
import * as api from "../../api/workbench";
import type { Card } from "../../api/workbenchTypes";
import { useWorkbenchStore } from "../../store/useWorkbenchStore";

type OriginFilter = "all" | "chapter" | "topic";
const EMPTY_CARDS: Card[] = [];

export default function CardPool() {
  const [searchParams] = useSearchParams();
  const targetCardId = searchParams.get("cardId");
  const { cardsByCourse, loadCourseCards, loadCourses, selectedCourseId } = useWorkbenchStore();
  const [cards, setCards] = useState<Card[]>([]);
  const [cardsLoaded, setCardsLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [origin, setOrigin] = useState<OriginFilter>("all");
  const [tag, setTag] = useState("all");
  const [favoritesOnly, setFavoritesOnly] = useState(false);
  const [editing, setEditing] = useState<Card | null>(null);
  const [saving, setSaving] = useState(false);
  const [highlightedCardId, setHighlightedCardId] = useState<string | null>(null);
  const [routeMessage, setRouteMessage] = useState<string | null>(null);
  const cardRefs = useRef(new Map<string, HTMLElement>());
  const locatedTarget = useRef<string | null>(null);

  const storedCards = selectedCourseId ? (cardsByCourse[selectedCourseId] ?? EMPTY_CARDS) : EMPTY_CARDS;
  useEffect(() => { setCards(storedCards); }, [storedCards]);
  useEffect(() => { loadCourses().catch((reason: unknown) => setError(reason instanceof Error ? reason.message : "课程加载失败")); }, [loadCourses]);
  useEffect(() => {
    if (!selectedCourseId) return;
    setCardsLoaded(false);
    loadCourseCards(selectedCourseId)
      .then(setCards)
      .catch((reason: unknown) => setError(reason instanceof Error ? reason.message : "卡片加载失败"))
      .finally(() => setCardsLoaded(true));
  }, [loadCourseCards, selectedCourseId]);

  const tags = useMemo(() => [...new Set(cards.flatMap((card) => card.tags))].sort(), [cards]);
  const visibleCards = useMemo(() => {
    const keyword = query.trim().toLocaleLowerCase();
    return cards.filter((card) =>
      (origin === "all" || card.origin_type === origin) &&
      (tag === "all" || card.tags.includes(tag)) &&
      (!favoritesOnly || card.favorite) &&
      (!keyword || [card.title, card.content, card.origin_title, ...card.tags].some((value) => value.toLocaleLowerCase().includes(keyword)))
    );
  }, [cards, favoritesOnly, origin, query, tag]);

  useEffect(() => {
    if (!targetCardId) {
      locatedTarget.current = null;
      setHighlightedCardId(null);
      setRouteMessage(null);
      return;
    }
    const target = cards.find((card) => card.id === targetCardId);
    if (!target) {
      if (!selectedCourseId) setRouteMessage("请先选择课程，再定位指定卡片");
      else if (cardsLoaded) setRouteMessage("未找到指定卡片，卡片可能已被删除或不属于当前课程");
      return;
    }
    if (locatedTarget.current === targetCardId) return;
    locatedTarget.current = targetCardId;
    const hidden = !visibleCards.some((card) => card.id === targetCardId);
    if (hidden) {
      setQuery("");
      setOrigin("all");
      setTag("all");
      setFavoritesOnly(false);
      setRouteMessage(`已调整筛选并定位到“${target.title}”`);
    } else {
      setRouteMessage(`已定位到“${target.title}”`);
    }
    setHighlightedCardId(targetCardId);
  }, [cards, cardsLoaded, selectedCourseId, targetCardId, visibleCards]);

  useEffect(() => {
    if (!highlightedCardId || !visibleCards.some((card) => card.id === highlightedCardId)) return;
    const target = cardRefs.current.get(highlightedCardId);
    if (!target) return;
    target.scrollIntoView({ behavior: "smooth", block: "center" });
    target.focus({ preventScroll: true });
  }, [highlightedCardId, visibleCards]);

  const replaceCard = (updated: Card) => setCards((current) => current.map((card) => card.id === updated.id ? updated : card));
  const toggleFavorite = async (card: Card) => {
    setError(null);
    try { replaceCard(await api.setCourseCardFavorite(card.id, !card.favorite, card.updated_at)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "收藏更新失败"); }
  };
  const saveEdit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!editing) return;
    const data = new FormData(event.currentTarget);
    setSaving(true); setError(null);
    try {
      const updated = await api.updateCourseCard(editing.id, {
        title: String(data.get("title") ?? "").trim(), content: String(data.get("content") ?? ""),
        tags: String(data.get("tags") ?? "").split(/[,，]/).map((item) => item.trim()).filter(Boolean),
        status: data.get("status") === "ARCHIVED" ? "ARCHIVED" : "ACTIVE",
        expected_updated_at: editing.updated_at,
      });
      replaceCard(updated); setEditing(null);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "卡片保存失败"); }
    finally { setSaving(false); }
  };

  return <div className="space-y-5 animate-in">
    <div><h1 className="text-xl font-semibold text-zinc-900">课程卡片池</h1><p className="mt-1 text-sm text-zinc-500">章节精读与融合精读沉淀的卡片会汇总到这里。</p></div>
    <div className="flex flex-wrap items-center gap-2 border-y border-zinc-200 py-3">
      <label className="relative min-w-60 flex-1"><Search className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-zinc-400"/><span className="sr-only">搜索课程卡片</span><input type="search" aria-label="搜索课程卡片" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索标题、正文、来源或标签" className="h-9 w-full border border-zinc-300 bg-white pl-9 pr-3 text-sm outline-none focus:border-zinc-500"/></label>
      <div className="inline-flex border border-zinc-200 bg-zinc-50 p-0.5" role="group" aria-label="卡片来源筛选">{([['all','全部'],['chapter','章节精读'],['topic','融合精读']] as const).map(([value,label]) => <button key={value} type="button" onClick={() => setOrigin(value)} aria-pressed={origin === value} className={`h-8 px-3 text-sm ${origin === value ? "bg-white font-medium shadow-sm" : "text-zinc-500"}`}>{label}</button>)}</div>
      <select aria-label="标签筛选" value={tag} onChange={(event) => setTag(event.target.value)} className="h-9 border border-zinc-300 bg-white px-3 text-sm"><option value="all">全部标签</option>{tags.map((item) => <option key={item}>{item}</option>)}</select>
      <button type="button" aria-pressed={favoritesOnly} onClick={() => setFavoritesOnly((value) => !value)} className={`inline-flex h-9 items-center gap-1.5 border px-3 text-sm ${favoritesOnly ? "border-amber-300 bg-amber-50 text-amber-800" : "border-zinc-300 bg-white text-zinc-600"}`}><Star className="h-4 w-4"/>仅看收藏</button>
    </div>
    {error && <p role="alert" className="border border-red-100 bg-red-50 px-3 py-2 text-sm text-red-600">{error}</p>}
    {routeMessage && <p role="status" aria-live="polite" className="border-l-2 border-emerald-500 bg-emerald-50 px-3 py-2 text-sm text-emerald-800">{routeMessage}</p>}
    {cards.length === 0 ? <EmptyCourse/> : visibleCards.length === 0 ? <div className="border border-dashed border-zinc-300 bg-white px-8 py-12 text-center"><p className="text-sm font-medium text-zinc-700">没有符合条件的卡片</p><button type="button" onClick={() => { setQuery(""); setOrigin("all"); setTag("all"); setFavoritesOnly(false); }} className="mt-3 text-sm text-emerald-700 underline">清除筛选</button></div> :
      <div className="grid gap-3 md:grid-cols-2">{visibleCards.map((card) => <article key={card.id} id={`card-${card.id}`} ref={(node) => { if (node) cardRefs.current.set(card.id, node); else cardRefs.current.delete(card.id); }} tabIndex={-1} aria-label={`卡片：${card.title}`} data-highlighted={highlightedCardId === card.id ? "true" : undefined} className={`border bg-white p-4 outline-none transition-shadow ${highlightedCardId === card.id ? "border-emerald-500 ring-2 ring-emerald-200" : "border-zinc-200"}`}>
        <div className="flex items-start justify-between gap-3"><div className="min-w-0"><h2 className="truncate text-sm font-medium text-zinc-900">{card.title}</h2><div className="mt-1 flex flex-wrap gap-1">{card.tags.map((item) => <span key={item} className="bg-zinc-100 px-1.5 py-0.5 text-xs text-zinc-500">{item}</span>)}{card.status === "ARCHIVED" && <span className="bg-zinc-200 px-1.5 py-0.5 text-xs text-zinc-600">已归档</span>}</div></div><div className="flex shrink-0"><button type="button" title={card.favorite ? "取消收藏" : "收藏"} aria-label={card.favorite ? "取消收藏" : "收藏"} onClick={() => void toggleFavorite(card)} className="p-1.5 text-zinc-500 hover:text-amber-600"><Star className={`h-4 w-4 ${card.favorite ? "fill-amber-400 text-amber-500" : ""}`}/></button><button type="button" title="编辑卡片" aria-label={`编辑 ${card.title}`} onClick={() => setEditing(card)} className="p-1.5 text-zinc-500 hover:text-zinc-900"><Pencil className="h-4 w-4"/></button></div></div>
        <p className="mt-3 whitespace-pre-wrap text-sm leading-6 text-zinc-600">{card.content}</p><div className="mt-3 flex items-center justify-between gap-3"><span className="text-xs text-zinc-400">{card.card_type}</span><Link to={card.origin_type === "chapter" ? `/workbench/chapter?chapterId=${card.origin_id}` : `/workbench/courses/${selectedCourseId}/fusion/${card.origin_id}`} className="text-xs text-emerald-700 underline">{card.origin_title}</Link></div>
      </article>)}</div>}
    {editing && <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4" role="dialog" aria-modal="true" aria-labelledby="card-edit-title"><form onSubmit={saveEdit} className="w-full max-w-xl border border-zinc-200 bg-white shadow-xl"><div className="flex items-center justify-between border-b px-5 py-3"><h2 id="card-edit-title" className="text-base font-semibold">编辑卡片</h2><button type="button" aria-label="关闭" onClick={() => setEditing(null)}><X className="h-5 w-5"/></button></div><div className="space-y-4 p-5"><label className="block text-sm">标题<input name="title" required maxLength={200} defaultValue={editing.title} className="mt-1 h-9 w-full border border-zinc-300 px-3"/></label><label className="block text-sm">正文<textarea name="content" required defaultValue={editing.content} rows={8} className="mt-1 w-full resize-y border border-zinc-300 p-3"/></label><label className="block text-sm">标签<input name="tags" defaultValue={editing.tags.join("，")} placeholder="使用逗号分隔" className="mt-1 h-9 w-full border border-zinc-300 px-3"/></label><label className="block text-sm">状态<select name="status" defaultValue={editing.status} className="mt-1 h-9 w-full border border-zinc-300 px-3"><option value="ACTIVE">有效</option><option value="ARCHIVED">归档</option></select></label></div><div className="flex justify-end gap-2 border-t px-5 py-3"><button type="button" onClick={() => setEditing(null)} className="h-9 border border-zinc-300 px-4 text-sm">取消</button><button type="submit" disabled={saving} className="h-9 bg-zinc-900 px-4 text-sm text-white disabled:opacity-50">{saving ? "保存中" : "保存"}</button></div></form></div>}
  </div>;
}

function EmptyCourse() { return <div className="border border-dashed border-zinc-300 bg-white px-8 py-12 text-center"><p className="text-sm font-medium text-zinc-700">本课程还没有卡片</p><p className="mt-1 text-xs text-zinc-400">完成章节精读或融合精读后，卡片会显示在这里。</p><Link to="/workbench/chapter" className="mt-4 inline-flex bg-zinc-900 px-4 py-2 text-sm font-medium text-white">返回章节精读</Link></div>; }
