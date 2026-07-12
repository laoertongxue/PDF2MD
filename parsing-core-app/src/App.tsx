import { useEffect } from "react";
import { HashRouter, Navigate, Routes, Route, useParams } from "react-router-dom";
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
import TopicMap from "./components/workbench/TopicMap";
import TopicFusion from "./components/workbench/TopicFusion";
import { useWorkbenchStore } from "./store/useWorkbenchStore";

function CourseTopicRoute() {
  const { courseId, topicId } = useParams();
  const selectCourse = useWorkbenchStore((state) => state.selectCourse);
  useEffect(() => { if (courseId) selectCourse(courseId); }, [courseId, selectCourse]);
  return topicId ? <TopicMap initialTopicId={topicId} oldResult /> : <TopicMap />;
}

function CourseFusionRoute() {
  const { courseId, topicId } = useParams();
  const selectCourse = useWorkbenchStore((state) => state.selectCourse);
  const loadTopics = useWorkbenchStore((state) => state.loadTopics);
  const topics = useWorkbenchStore((state) => courseId ? (state.topicsByCourse[courseId] ?? []) : []);
  useEffect(() => {
    if (!courseId) return;
    selectCourse(courseId);
    void loadTopics(courseId).catch(() => undefined);
  }, [courseId, loadTopics, selectCourse]);
  if (!courseId) return <Navigate to="/workbench" replace />;
  if (!topicId && topics[0]) return <Navigate to={`/workbench/courses/${courseId}/fusion/${topics[0].id}`} replace />;
  return topicId ? <TopicFusion courseId={courseId} topicId={topicId} /> : <div className="py-16 text-center text-sm text-zinc-500">请先在课程主题中创建并确认主题</div>;
}

export default function App() {
  return (
    <HashRouter>
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
          <Route path="/workbench/courses/:courseId/topics" element={<CourseTopicRoute />} />
          <Route path="/workbench/courses/:courseId/topics/:topicId" element={<CourseTopicRoute />} />
          <Route path="/workbench/courses/:courseId/fusion" element={<CourseFusionRoute />} />
          <Route path="/workbench/courses/:courseId/fusion/:topicId" element={<CourseFusionRoute />} />
          <Route path="/doc/:taskId" element={<DocViewer />} />
        </Route>
      </Routes>
    </HashRouter>
  );
}
