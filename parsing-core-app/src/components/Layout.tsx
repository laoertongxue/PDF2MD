import { Link, Outlet, useLocation } from "react-router-dom";
import { LayoutDashboard, PlusCircle, FileText, Terminal, BookOpen } from "lucide-react";

const nav = [
  { to: "/", label: "仪表盘", icon: LayoutDashboard },
  { to: "/submit", label: "新建批次", icon: PlusCircle },
  { to: "/workbench", label: "课程精读", icon: BookOpen },
];

export default function Layout() {
  const { pathname } = useLocation();

  return (
    <div className="flex h-screen overflow-hidden">
      {/* Sidebar */}
      <aside className="w-56 shrink-0 flex flex-col border-r border-zinc-200 bg-white">
        {/* Logo */}
        <div className="h-14 flex items-center gap-2.5 px-5 border-b border-zinc-100">
          <div className="flex h-7 w-7 items-center justify-center rounded-md bg-zinc-900 text-white">
            <FileText size={15} strokeWidth={2} />
          </div>
          <span className="font-semibold text-sm text-zinc-900">PDF2MD</span>
        </div>

        {/* Nav */}
        <nav className="flex-1 px-3 py-4 space-y-0.5">
          {nav.map(({ to, label, icon: Icon }) => {
            const active = pathname === to || (to !== "/" && pathname.startsWith(to));
            return (
              <Link
                key={to}
                to={to}
                className={`flex items-center gap-2.5 px-3 py-2 rounded-md text-sm transition-colors ${
                  active
                    ? "bg-zinc-100 text-zinc-900 font-medium"
                    : "text-zinc-500 hover:text-zinc-700 hover:bg-zinc-50"
                }`}
              >
                <Icon size={17} strokeWidth={active ? 2 : 1.5} />
                {label}
              </Link>
            );
          })}
        </nav>

        {/* Footer */}
        <div className="px-4 py-3 border-t border-zinc-100">
          <a
            href="http://127.0.0.1:8000/health"
            target="_blank"
            rel="noopener"
            className="flex items-center gap-2 text-xs text-zinc-400 hover:text-zinc-600 transition-colors"
          >
            <span className="relative flex h-2 w-2">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-75" />
              <span className="relative inline-flex h-2 w-2 rounded-full bg-emerald-500" />
            </span>
            服务运行中 :8000
          </a>
        </div>
      </aside>

      {/* Main content area */}
      <div className="flex-1 flex flex-col min-w-0 overflow-hidden">
        {/* Top bar */}
        <header className="h-14 shrink-0 border-b border-zinc-200 bg-white/80 backdrop-blur-sm flex items-center px-6">
          <div className="flex items-center gap-2 text-sm text-zinc-500">
            <Terminal size={15} />
            <span className="font-mono text-xs">parsing-core</span>
          </div>
        </header>

        {/* Page */}
        <main className="flex-1 overflow-y-auto">
          <div className="max-w-4xl mx-auto px-8 py-8">
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  );
}
