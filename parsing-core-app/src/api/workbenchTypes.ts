export interface Course {
  id: string;
  title: string;
  description: string;
  root_dir: string;
  created_at?: number;
  updated_at?: number;
}

export interface Source {
  id: string;
  course_id: string;
  kind: string;
  file_path: string;
  title: string;
  markdown_path?: string | null;
  status: string;
  created_at?: number;
  updated_at?: number;
}

export interface Chapter {
  id: string;
  source_id: string;
  course_id: string;
  seq: number;
  title: string;
  source_md_path?: string;
  status: string;
  created_at?: number;
  updated_at?: number;
}

export interface Card {
  id: string;
  course_id: string;
  chapter_id: string;
  kind: string;
  title: string;
  body: string;
  favorite: boolean;
  created_at?: number;
  updated_at?: number;
}

export interface NoteBlock {
  id: string;
  chapter_id: string;
  kind: string;
  title: string;
  body: string;
  seq: number;
  updated_at?: number;
}

export interface WorkbenchSettings {
  deepseek_model: string;
  deepseek_key_masked: string | null;
}
