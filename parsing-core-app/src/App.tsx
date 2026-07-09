import { BrowserRouter, Routes, Route } from "react-router-dom";
import Layout from "./components/Layout";
import Dashboard from "./components/Dashboard";
import BatchSubmit from "./components/BatchSubmit";
import DocViewer from "./components/DocViewer";
import CourseList from "./components/workbench/CourseList";

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route path="/" element={<Dashboard />} />
          <Route path="/submit" element={<BatchSubmit />} />
          <Route path="/workbench" element={<CourseList />} />
          <Route path="/doc/:taskId" element={<DocViewer />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}
