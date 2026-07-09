import { create } from "zustand";
import * as api from "../api/workbench";
import type { Card, Chapter, Course, NoteBlock, Source } from "../api/workbenchTypes";

interface WorkbenchState {
  courses: Course[];
  sources: Record<string, Source[]>;
  chapters: Record<string, Chapter[]>;
  cardsByCourse: Record<string, Card[]>;
  noteBlocksByChapter: Record<string, NoteBlock[]>;
  selectedCourseId: string | null;

  selectCourse: (courseId: string) => void;
  loadCourses: () => Promise<void>;
  loadSources: (courseId: string) => Promise<Source[]>;
  loadChapters: (sourceId: string) => Promise<Chapter[]>;
  loadCourseCards: (courseId: string) => Promise<Card[]>;
  loadChapterNoteBlocks: (chapterId: string) => Promise<NoteBlock[]>;
  createCourse: (title: string, description: string, rootDir: string) => Promise<Course>;
  addSource: (courseId: string, filePath: string, title: string, kind?: string) => Promise<Source>;
  detectChapters: (sourceId: string) => Promise<Chapter[]>;
  confirmChapter: (chapterId: string) => Promise<Chapter>;
  runChapter: (chapterId: string) => Promise<void>;
}

export const useWorkbenchStore = create<WorkbenchState>((set, get) => {
  const updateChapter = (chapter: Chapter) =>
    set((state) => {
      const chapters = state.chapters[chapter.source_id] ?? [];
      const exists = chapters.some((item) => item.id === chapter.id);
      return {
        chapters: {
          ...state.chapters,
          [chapter.source_id]: exists ? chapters.map((item) => (item.id === chapter.id ? chapter : item)) : [...chapters, chapter],
        },
      };
    });

  return {
    courses: [],
    sources: {},
    chapters: {},
    cardsByCourse: {},
    noteBlocksByChapter: {},
    selectedCourseId: null,

    selectCourse: (courseId) => set({ selectedCourseId: courseId }),

    loadCourses: async () => {
      const courses = await api.listCourses();
      set((state) => ({ courses, selectedCourseId: state.selectedCourseId ?? courses[0]?.id ?? null }));
    },

    loadSources: async (courseId) => {
      const sources = await api.listSources(courseId);
      set((state) => ({ sources: { ...state.sources, [courseId]: sources } }));
      return sources;
    },

    loadChapters: async (sourceId) => {
      const chapters = await api.listChapters(sourceId);
      set((state) => ({ chapters: { ...state.chapters, [sourceId]: chapters } }));
      return chapters;
    },

    loadCourseCards: async (courseId) => {
      const cards = await api.listCourseCards(courseId);
      set((state) => ({ cardsByCourse: { ...state.cardsByCourse, [courseId]: cards } }));
      return cards;
    },

    loadChapterNoteBlocks: async (chapterId) => {
      const blocks = await api.listChapterNoteBlocks(chapterId);
      set((state) => ({ noteBlocksByChapter: { ...state.noteBlocksByChapter, [chapterId]: blocks } }));
      return blocks;
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
      let updated: Chapter | null = null;
      try {
        updated = await api.runChapter(chapterId);
      } catch (error) {
        updated = await api.getChapter(chapterId).catch(() => null);
        if (updated) updateChapter(updated);
        throw error;
      }
      updateChapter(updated);
      const blocks = await api.listChapterNoteBlocks(chapterId);
      const chapter = Object.values(get().chapters).flat().find((item) => item.id === chapterId) ?? updated;
      await get().loadCourseCards(chapter.course_id);
      set((state) => ({ noteBlocksByChapter: { ...state.noteBlocksByChapter, [chapterId]: blocks } }));
    },
  };
});
