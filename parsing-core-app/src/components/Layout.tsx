import { Link, Outlet, useLocation } from "react-router-dom";

export default function Layout() {
  const location = useLocation();
  const tabs = [
    { to: "/", label: "仪表盘" },
    { to: "/submit", label: "新建批次" },
  ];

  return (
    <div className="min-h-screen bg-gray-50">
      <header className="bg-white border-b sticky top-0 z-10">
        <div className="max-w-6xl mx-auto px-4 h-12 flex items-center gap-6">
          <h1 className="font-bold text-lg">parsing-core</h1>
          <nav className="flex gap-1">
            {tabs.map((t) => (
              <Link
                key={t.to}
                to={t.to}
                className={`px-3 py-1 rounded text-sm ${location.pathname === t.to ? "bg-gray-100 font-medium" : "text-gray-500 hover:text-gray-700"}`}
              >
                {t.label}
              </Link>
            ))}
          </nav>
        </div>
      </header>
      <main className="max-w-6xl mx-auto px-4 py-6">
        <Outlet />
      </main>
    </div>
  );
}
