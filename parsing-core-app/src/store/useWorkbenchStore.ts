import { create } from "zustand";
import * as api from "../api/workbench";
import type { Chapter, Course, Source } from "../api/workbenchTypes";

interface WorkbenchState {
  courses: Course[];
  sources: Record<string, Source[]>;
  chapters: Record<string, Chapter[]>;
  selectedCourseId: string | null;

  loadCourses: () => Promise<void>;
  createCourse: (title: string, description: string, rootDir: string) => Promise<Course>;
  addSource: (courseId: string, filePath: string, title: string, kind?: string) => Promise<Source>;
  detectChapters: (sourceId: string) => Promise<Chapter[]>;
  confirmChapter: (chapterId: string) => Promise<Chapter>;
  runChapter: (chapterId: string) => Promise<void>;
}

export const useWorkbenchStore = create<WorkbenchState>((set) => ({
  courses: [],
  sources: {},
  chapters: {},
  selectedCourseId: null,

  loadCourses: async () => {
    const courses = await api.listCourses();
    set((state) => ({ courses, selectedCourseId: state.selectedCourseId ?? courses[0]?.id ?? null }));
  },

  createCourse: async (title, description, rootDir) => {
    const course = await api.createCourse(title, description, rootDir);
    set((state) => ({ courses: [course, ...state.courses], selectedCourseId: course.id }));
    return course;
  },

  addSource: async (courseId, filePath, title, kind = "main") => {
    const source = await api.createSource(courseId, filePath, title, kind);
    set((state) => ({ sources: { ...state.sources, [courseId]: [source, ...(state.sources[courseId] ?? [])] } }));
    return source;
  },

  detectChapters: async (sourceId) => {
    const chapters = await api.detectChapters(sourceId);
    set((state) => ({ chapters: { ...state.chapters, [sourceId]: chapters } }));
    return chapters;
  },

  confirmChapter: async (chapterId) => {
    const chapter = await api.confirmChapter(chapterId);
    set((state) => ({
      chapters: {
        ...state.chapters,
        [chapter.source_id]: (state.chapters[chapter.source_id] ?? []).map((item) => (item.id === chapter.id ? chapter : item)),
      },
    }));
    return chapter;
  },

  runChapter: async (chapterId) => {
    await api.runChapter(chapterId);
  },
}));
