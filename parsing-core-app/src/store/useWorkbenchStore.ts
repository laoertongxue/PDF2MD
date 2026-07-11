import { create } from "zustand";
import * as api from "../api/workbench";
import type {
  Card,
  Chapter,
  Course,
  CourseTopic,
  NoteBlock,
  Source,
  TopicCard,
  TopicNoteBlock,
  TopicOutlineExecutor,
  TopicCreateRequest,
  TopicPatchRequest,
  TopicMergeRequest,
  TopicSplitRequest,
  TopicRun,
} from "../api/workbenchTypes";

interface AsyncActionState {
  loading: boolean;
  error: string | null;
}

interface WorkbenchState {
  courses: Course[];
  sources: Record<string, Source[]>;
  chapters: Record<string, Chapter[]>;
  cardsByCourse: Record<string, Card[]>;
  noteBlocksByChapter: Record<string, NoteBlock[]>;
  topicsByCourse: Record<string, CourseTopic[]>;
  topicBlocksById: Record<string, TopicNoteBlock[]>;
  topicCardsById: Record<string, TopicCard[]>;
  topicRunsById: Record<string, TopicRun[]>;
  deletedTopics: Record<string, true>;
  topicActions: Record<string, AsyncActionState>;
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
  runHybridChapter: (chapterId: string) => Promise<void>;
  loadTopics: (courseId: string) => Promise<CourseTopic[]>;
  generateTopics: (courseId: string, executor?: TopicOutlineExecutor) => Promise<CourseTopic[]>;
  createTopic: (courseId: string, body: TopicCreateRequest) => Promise<CourseTopic>;
  patchTopic: (topicId: string, body: TopicPatchRequest) => Promise<CourseTopic>;
  mergeTopics: (courseId: string, body: TopicMergeRequest) => Promise<CourseTopic>;
  splitTopic: (topicId: string, body: TopicSplitRequest) => Promise<CourseTopic[]>;
  updateTopicMapping: (topicId: string, chapterIds: string[]) => Promise<CourseTopic>;
  confirmTopics: (courseId: string) => Promise<CourseTopic[]>;
  reorderTopics: (courseId: string, topicIds: string[]) => Promise<CourseTopic[]>;
  deleteTopic: (courseId: string, topicId: string) => Promise<void>;
  runTopic: (topicId: string) => Promise<CourseTopic>;
  runTopicHybrid: (topicId: string) => Promise<CourseTopic>;
  loadTopicBlocks: (topicId: string) => Promise<TopicNoteBlock[]>;
  loadTopicCards: (topicId: string) => Promise<TopicCard[]>;
  loadTopicRuns: (topicId: string) => Promise<TopicRun[]>;
  retryTopicSync: (topicId: string) => Promise<CourseTopic>;
  recoverTopic: (topicId: string) => Promise<CourseTopic>;
}

export const useWorkbenchStore = create<WorkbenchState>((set, get) => {
  const actionVersions = new Map<string, number>();
  const resourceEpochs = new Map<string, number>();
  let actionSequence = 0;

  const claimResource = (resourceKey: string, startedAt: number, requireUnchanged = false) => {
    if (requireUnchanged && (resourceEpochs.get(resourceKey) ?? 0) > startedAt) return null;
    resourceEpochs.set(resourceKey, startedAt);
    return startedAt;
  };

  const findTopicCourseId = (topicId: string) => {
    for (const [courseId, topics] of Object.entries(get().topicsByCourse)) {
      if (topics.some((topic) => topic.id === topicId)) return courseId;
    }
    return null;
  };

  const runAction = async <T>(
    actionKey: string,
    resourceKeys: string[],
    operation: () => Promise<T>,
    apply: (result: T) => void,
    responseResourceKeys?: (result: T) => string[],
  ): Promise<T> => {
    const startedAt = ++actionSequence;
    const actionVersion = (actionVersions.get(actionKey) ?? 0) + 1;
    actionVersions.set(actionKey, actionVersion);
    const leases = new Map(resourceKeys.map((key) => [key, claimResource(key, startedAt)]));
    const ownsResources = () =>
      [...leases].every(([resourceKey, epoch]) => resourceEpochs.get(resourceKey) === epoch);
    set((state) => ({
      topicActions: { ...state.topicActions, [actionKey]: { loading: true, error: null } },
    }));
    try {
      const result = await operation();
      if (ownsResources()) {
        for (const resourceKey of responseResourceKeys?.(result) ?? []) {
          if (!leases.has(resourceKey)) {
            const lease = claimResource(resourceKey, startedAt, true);
            if (lease === null) return result;
            leases.set(resourceKey, lease);
          }
        }
        if (ownsResources()) apply(result);
      }
      return result;
    } catch (error) {
      const message = api.getSafeApiErrorMessage(error) ?? "操作失败，请稍后重试";
      if (actionVersions.get(actionKey) === actionVersion) {
        set((state) => ({
          topicActions: { ...state.topicActions, [actionKey]: { loading: false, error: message } },
        }));
      }
      throw new Error(message);
    } finally {
      if (actionVersions.get(actionKey) === actionVersion) {
        set((state) => ({
          topicActions: {
            ...state.topicActions,
            [actionKey]: { ...state.topicActions[actionKey], loading: false },
          },
        }));
      }
    }
  };

  const saveTopics = (courseId: string, topics: CourseTopic[]) =>
    set((state) => ({
      topicsByCourse: {
        ...state.topicsByCourse,
        [courseId]: topics.filter((topic) => !state.deletedTopics[topic.id]),
      },
    }));

  const saveTopic = (topic: CourseTopic) =>
    set((state) => {
      if (state.deletedTopics[topic.id]) return state;
      const topics = state.topicsByCourse[topic.course_id] ?? [];
      const exists = topics.some((item) => item.id === topic.id);
      return {
        topicsByCourse: {
          ...state.topicsByCourse,
          [topic.course_id]: exists
            ? topics.map((item) => (item.id === topic.id ? topic : item))
            : [...topics, topic].sort((left, right) => left.seq - right.seq),
        },
      };
    });

  const finalizeTopicDeletion = (courseId: string, topicId: string) => {
    const finalizedAt = ++actionSequence;
    for (const resourceKey of [
      `courseTopics:${courseId}`,
      `topic:${topicId}`,
      `topicBlocks:${topicId}`,
      `topicCards:${topicId}`,
      `topicRuns:${topicId}`,
    ]) resourceEpochs.set(resourceKey, finalizedAt);
    set((state) => {
      const topicBlocksById = { ...state.topicBlocksById };
      const topicCardsById = { ...state.topicCardsById };
      const topicRunsById = { ...state.topicRunsById };
      delete topicBlocksById[topicId];
      delete topicCardsById[topicId];
      delete topicRunsById[topicId];
      return {
        deletedTopics: { ...state.deletedTopics, [topicId]: true },
        topicsByCourse: {
          ...state.topicsByCourse,
          [courseId]: (state.topicsByCourse[courseId] ?? []).filter((topic) => topic.id !== topicId),
        },
        topicBlocksById,
        topicCardsById,
        topicRunsById,
      };
    });
  };

  const runTopicAction = (
    action: string,
    topicId: string,
    operation: () => Promise<CourseTopic>,
  ) => {
    const courseId = findTopicCourseId(topicId);
    return runAction(
      `${action}:${topicId}`,
      [`topic:${topicId}`, ...(courseId ? [`courseTopics:${courseId}`] : [])],
      operation,
      saveTopic,
      (topic) => [`courseTopics:${topic.course_id}`],
    );
  };

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

  const refreshChapterArtifacts = async (chapter: Chapter) => {
    updateChapter(chapter);
    await Promise.all([get().loadChapterNoteBlocks(chapter.id), get().loadCourseCards(chapter.course_id)]);
  };

  const runChapterWith = async (chapterId: string, runner: (chapterId: string) => Promise<Chapter>) => {
    try {
      const updated = await runner(chapterId);
      await refreshChapterArtifacts(updated);
    } catch (error) {
      const updated = await api.getChapter(chapterId).catch(() => null);
      if (updated) updateChapter(updated);
      throw error;
    }
  };

  return {
    courses: [],
    sources: {},
    chapters: {},
    cardsByCourse: {},
    noteBlocksByChapter: {},
    topicsByCourse: {},
    topicBlocksById: {},
    topicCardsById: {},
    topicRunsById: {},
    deletedTopics: {},
    topicActions: {},
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
      updateChapter(chapter);
      return chapter;
    },

    runChapter: async (chapterId) => {
      await runChapterWith(chapterId, (id) => api.runChapter(id));
    },

    runHybridChapter: async (chapterId) => {
      await runChapterWith(chapterId, api.runHybridChapter);
    },

    loadTopics: (courseId) =>
      runAction(
        `loadTopics:${courseId}`,
        [`courseTopics:${courseId}`],
        () => api.listTopics(courseId),
        (topics) => saveTopics(courseId, topics),
      ),

    generateTopics: (courseId, executor = "stub") =>
      runAction(
        `generateTopics:${courseId}`,
        [`courseTopics:${courseId}`],
        () => api.generateTopics(courseId, executor),
        (topics) => saveTopics(courseId, topics),
      ),

    createTopic: (courseId, body) =>
      runAction(
        `createTopic:${courseId}`,
        [`courseTopics:${courseId}`],
        () => api.createTopic(courseId, body),
        saveTopic,
      ),

    patchTopic: (topicId, body) =>
      runTopicAction("patchTopic", topicId, () => api.patchTopic(topicId, body)),

    mergeTopics: (courseId, body) =>
      runAction(
        `mergeTopics:${courseId}`,
        [`courseTopics:${courseId}`, ...body.topic_ids.map((id) => `topic:${id}`)],
        () => api.mergeTopics(courseId, body),
        (merged) => {
          for (const topicId of body.topic_ids) finalizeTopicDeletion(courseId, topicId);
          saveTopic(merged);
        },
        (merged) => [`topic:${merged.id}`, `courseTopics:${merged.course_id}`],
      ),

    splitTopic: (topicId, body) => {
      const courseId = findTopicCourseId(topicId);
      return runAction(
        `splitTopic:${topicId}`,
        [`topic:${topicId}`, ...(courseId ? [`courseTopics:${courseId}`] : [])],
        () => api.splitTopic(topicId, body),
        (topics) => topics.forEach(saveTopic),
        (topics) => topics.flatMap((topic) => [`topic:${topic.id}`, `courseTopics:${topic.course_id}`]),
      );
    },

    updateTopicMapping: (topicId, chapterIds) =>
      runTopicAction(
        "updateTopicMapping",
        topicId,
        () => api.updateTopicMapping(topicId, chapterIds),
      ),

    confirmTopics: (courseId) =>
      runAction(
        `confirmTopics:${courseId}`,
        [`courseTopics:${courseId}`],
        () => api.confirmTopics(courseId),
        (topics) => saveTopics(courseId, topics),
      ),

    reorderTopics: (courseId, topicIds) =>
      runAction(
        `reorderTopics:${courseId}`,
        [`courseTopics:${courseId}`],
        () => api.reorderTopics(courseId, topicIds),
        (topics) => saveTopics(courseId, topics),
      ),

    deleteTopic: (courseId, topicId) =>
      (async () => {
        const actionKey = `deleteTopic:${topicId}`;
        const actionVersion = (actionVersions.get(actionKey) ?? 0) + 1;
        actionVersions.set(actionKey, actionVersion);
        set((state) => ({ topicActions: { ...state.topicActions, [actionKey]: { loading: true, error: null } } }));
        try {
          await api.deleteTopic(topicId);
          finalizeTopicDeletion(courseId, topicId);
        } catch (error) {
          const message = api.getSafeApiErrorMessage(error) ?? "操作失败，请稍后重试";
          if (actionVersions.get(actionKey) === actionVersion) {
            set((state) => ({ topicActions: { ...state.topicActions, [actionKey]: { loading: false, error: message } } }));
          }
          throw new Error(message);
        } finally {
          if (actionVersions.get(actionKey) === actionVersion) {
            set((state) => ({
              topicActions: { ...state.topicActions, [actionKey]: { ...state.topicActions[actionKey], loading: false } },
            }));
          }
        }
      })(),

    runTopic: (topicId) =>
      runTopicAction("runTopic", topicId, () => api.runTopic(topicId)),

    runTopicHybrid: (topicId) =>
      runTopicAction("runTopicHybrid", topicId, () => api.runTopicHybrid(topicId)),

    loadTopicBlocks: (topicId) =>
      runAction(
        `loadTopicBlocks:${topicId}`,
        [`topicBlocks:${topicId}`],
        () => api.listTopicNoteBlocks(topicId),
        (blocks) => set((state) => ({ topicBlocksById: { ...state.topicBlocksById, [topicId]: blocks } })),
      ),

    loadTopicCards: (topicId) =>
      runAction(
        `loadTopicCards:${topicId}`,
        [`topicCards:${topicId}`],
        () => api.listTopicCards(topicId),
        (cards) => set((state) => ({ topicCardsById: { ...state.topicCardsById, [topicId]: cards } })),
      ),

    loadTopicRuns: (topicId) =>
      runAction(
        `loadTopicRuns:${topicId}`,
        [`topicRuns:${topicId}`],
        () => api.listTopicRuns(topicId),
        (runs) => set((state) => ({ topicRunsById: { ...state.topicRunsById, [topicId]: runs } })),
      ),

    retryTopicSync: (topicId) =>
      runTopicAction("retryTopicSync", topicId, () => api.retryTopicSync(topicId)),

    recoverTopic: (topicId) =>
      runTopicAction("recoverTopic", topicId, () => api.recoverTopic(topicId)),
  };
});
