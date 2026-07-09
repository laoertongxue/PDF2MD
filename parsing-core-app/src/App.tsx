import { BrowserRouter, Routes, Route } from "react-router-dom";
import Layout from "./components/Layout";
import Dashboard from "./components/Dashboard";
import BatchSubmit from "./components/BatchSubmit";
import DocViewer from "./components/DocViewer";
import CourseList from "./components/workbench/CourseList";
import SourceDetail from "./components/workbench/SourceDetail";
import ChapterConfirm from "./components/workbench/ChapterConfirm";
import ChapterWorkbench from "./components/workbench/ChapterWorkbench";
import CardPool from "./components/workbench/CardPool";
import Settings from "./components/workbench/Settings";

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route path="/" element={<Dashboard />} />
          <Route path="/submit" element={<BatchSubmit />} />
          <Route path="/workbench" element={<CourseList />} />
          <Route path="/workbench/settings" element={<Settings />} />
          <Route path="/workbench/source" element={<SourceDetail />} />
          <Route path="/workbench/chapters" element={<ChapterConfirm />} />
          <Route path="/workbench/chapter" element={<ChapterWorkbench />} />
          <Route path="/workbench/cards" element={<CardPool />} />
          <Route path="/doc/:taskId" element={<DocViewer />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}
