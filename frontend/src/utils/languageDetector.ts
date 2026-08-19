export type ReviewLanguage = 'python' | 'cpp';

export type LanguageDetection = {
  language: ReviewLanguage | null;
  error: string | null;
};

const extensionLanguages: Record<string, ReviewLanguage> = {
  '.py': 'python',
  '.pyw': 'python',
  '.cc': 'cpp',
  '.cpp': 'cpp',
  '.cxx': 'cpp',
  '.hh': 'cpp',
  '.hpp': 'cpp',
  '.hxx': 'cpp',
};

export function detectLanguage(content: string, filename?: string): LanguageDetection {
  const trimmed = content.trim();
  if (!trimmed) {
    return { language: null, error: '\u8bf7\u5148\u8f93\u5165\u4ee3\u7801\u6216\u6dfb\u52a0\u6587\u4ef6\u3002' };
  }

  const dot = filename?.lastIndexOf('.') ?? -1;
  const extension = dot >= 0 ? filename?.slice(dot).toLowerCase() : '';
  const extensionLanguage = extension ? extensionLanguages[extension] : undefined;
  if (extension && !extensionLanguage) {
    return { language: null, error: '\u4ec5\u652f\u6301 Python \u548c C++ \u6587\u4ef6\u3002' };
  }

  const unsupportedSyntax =
    /(^|\n)\s*(?:function\s+\w+\s*\(|(?:let|var)\s+\w+\s*=|console\.)/i.test(trimmed) ||
    /(^|\n)\s*(?:const|let|var)\s+\w+\s*=.*=>/i.test(trimmed) ||
    /(^|\n)\s*class\s+\w+\s*\{[\s\S]*\bconstructor\s*\(/i.test(trimmed);
  if (unsupportedSyntax) {
    return { language: null, error: '\u4ec5\u652f\u6301 Python \u548c C++ \u4ee3\u7801\u3002' };
  }

  const pythonScore =
    (/(^|\n)\s*(from\s+\S+\s+import|import\s+\S+)/m.test(trimmed) ? 3 : 0) +
    (/(^|\n)\s*(?:(?:async\s+)?def\s+\w+\s*\([^)]*\)(?:\s*->\s*[^:\n]+)?|class\s+\w+(?:\([^)]*\))?)\s*:/m.test(trimmed) ? 3 : 0) +
    (/\b(print|elif|None|True|False)\s*[( :]/.test(trimmed) ? 3 : 0) +
    (/\b(eval|exec|input)\s*\(/.test(trimmed) ? 3 : 0) +
    (/^\s*[A-Za-z_]\w*\s*=(?!=)[^;\n]*$/m.test(trimmed) ? 2 : 0) +
    (/^\s*[A-Za-z_]\w*\s*:\s*[^=\n]+(?:=\s*[^;\n]+)?$/m.test(trimmed) ? 3 : 0) +
    (/^\s{4,}\S+/m.test(trimmed) && /:\s*(\n|$)/m.test(trimmed) ? 1 : 0);

  const cppScore =
    (/#include\s*[<"]/.test(trimmed) ? 4 : 0) +
    (/\bstd::\w+|using\s+namespace\s+std\b/.test(trimmed) ? 3 : 0) +
    (/\b(?:int|void|auto|bool|string)\s+main\s*\(/.test(trimmed) ? 3 : 0) +
    (/\b[A-Za-z_]\w*(?:::\w+)*(?:<[^;{}\n]+>)?\s*[*&]?\s+\w+\s*\([^;{}]*\)\s*(?:const\s*)?\{/.test(trimmed) ? 3 : 0) +
    ((extensionLanguage === 'cpp' || /\b(?:public|private|protected)\s*:/.test(trimmed)) &&
    /\b(?:class|struct|enum)\s+\w+/.test(trimmed) ? 3 : 0) +
    (/[{}]\s*$|;\s*(?:\n|$)/m.test(trimmed) ? 1 : 0) +
    (/\b(?:int|void|char|float|double)\s+\w+\s*(?:=|;)/.test(trimmed) ? 1 : 0);

  let detected: ReviewLanguage | null = null;
  if (pythonScore >= 2 && pythonScore >= cppScore + 1) detected = 'python';
  if (cppScore >= 2 && cppScore >= pythonScore + 1) detected = 'cpp';

  if (!detected) {
    return { language: null, error: '\u65e0\u6cd5\u786e\u5b9a\u662f Python \u8fd8\u662f C++ \u4ee3\u7801\uff0c\u8bf7\u8865\u5145\u66f4\u660e\u786e\u7684\u4ee3\u7801\u3002' };
  }
  if (extensionLanguage && extensionLanguage !== detected) {
    return { language: null, error: '\u6587\u4ef6\u6269\u5c55\u540d\u4e0e\u4ee3\u7801\u5185\u5bb9\u4e0d\u4e00\u81f4\u3002' };
  }
  return { language: detected, error: null };
}
