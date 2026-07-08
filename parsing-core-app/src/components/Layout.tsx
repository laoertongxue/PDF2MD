import { Link, Outlet, useLocation } from "react-router-dom";
import { LayoutDashboard, PlusCircle, FileText, Activity } from "lucide-react";

export default function Layout() {
  const { pathname } = useLocation();

  return (
    <div className="min-h-screen flex flex-col">
      {/* Top bar */}
      <header className="sticky top-0 z-50 bg-white/80 backdrop-blur-xl border-b border-border/60">
        <div className="max-w-6xl mx-auto px-6 h-14 flex items-center justify-between">
          {/* Left */}
          <div className="flex items-center gap-8">
            <Link to="/" className="flex items-center gap-2.5 shrink-0">
              <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-accent text-white shadow-sm shadow-accent/25">
                <FileText size={17} strokeWidth={2} />
              </div>
              <span className="font-semibold text-[15px] tracking-tight text-gray-900">PDF2MD</span>
            </Link>
            <nav className="flex items-center gap-0.5">
              {[
                { to: "/", label: "仪表盘", Icon: LayoutDashboard },
                { to: "/submit", label: "新建批次", Icon: PlusCircle },
              ].map(({ to, label, Icon }) => {
                const active = pathname === to;
                return (
                  <Link
                    key={to}
                    to={to}
                    className={`inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[13px] font-medium transition-all duration-150 ${
                      active
                        ? "bg-gray-100/80 text-gray-900"
                        : "text-muted hover:text-gray-700 hover:bg-gray-50"
                    }`}
                  >
                    <Icon size={15} strokeWidth={2} />
                    {label}
                  </Link>
                );
              })}
            </nav>
          </div>

          {/* Right: status */}
          <div className="flex items-center gap-2 rounded-full bg-emerald-50 px-3 py-1.5 text-[12px] font-medium text-emerald-700">
            <span className="relative flex h-2 w-2">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-75" />
              <span className="relative inline-flex h-2 w-2 rounded-full bg-emerald-500" />
            </span>
            服务运行中
          </div>
        </div>
      </header>

      {/* Page content */}
      <main className="flex-1 max-w-6xl w-full mx-auto px-6 py-8">
        <Outlet />
      </main>
    </div>
  );
}
