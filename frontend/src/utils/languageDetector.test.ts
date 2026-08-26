import { describe, expect, it } from 'vitest';
import {
  canonicalPathKey,
  detectLanguage,
  duplicateCanonicalPaths,
  uniqueSnippetFilename,
  validateSourceText,
} from './languageDetector';

describe('detectLanguage', () => {
  it('detects Python from a supported filename', () => {
    expect(detectLanguage('print("ok")', 'review.py')).toEqual({ language: 'python', error: null });
  });

  it('detects C++ from a supported filename', () => {
    expect(detectLanguage('#include <iostream>\nint main() {}', 'main.cpp')).toEqual({ language: 'cpp', error: null });
  });

  it('detects Python from pasted syntax without a filename', () => {
    expect(detectLanguage('def greet(name):\n    return name\n')).toEqual({ language: 'python', error: null });
  });

  it('detects a Python function with a return annotation', () => {
    expect(detectLanguage('def normalize(value: str) -> str:\n    return value.strip()\n')).toEqual({
      language: 'python',
      error: null,
    });
  });

  it('detects C++ from pasted syntax without a filename', () => {
    expect(detectLanguage('#include <vector>\nint main() { return 0; }')).toEqual({ language: 'cpp', error: null });
  });

  it('detects minimal Python and C++ statements', () => {
    expect(detectLanguage('value = 1').language).toBe('python');
    expect(detectLanguage('int value = 1;').language).toBe('cpp');
  });

  it('rejects spoofed extensions and C-only extensions', () => {
    expect(detectLanguage('console.log("ok");', 'fake.py').language).toBeNull();
    expect(detectLanguage('SELECT * FROM users;', 'fake.py').language).toBeNull();
    expect(detectLanguage('int main() { return 0; }', 'main.c').language).toBeNull();
  });

  it('handles async Python, standalone C++ functions, and rejects JS-like assignments', () => {
    expect(detectLanguage('async def fetch():\n    await work()').language).toBe('python');
    expect(detectLanguage('int add(int a, int b) { return a + b; }').language).toBe('cpp');
    expect(detectLanguage('total = price * quantity;').language).toBeNull();
  });

  it('accepts common typed Python and C++ project files', () => {
    expect(detectLanguage('answer: int = 42', 'settings.py').language).toBe('python');
    expect(detectLanguage('class Widget { public: Widget(); };', 'widget.hpp').language).toBe('cpp');
    expect(detectLanguage('Widget make_widget() { return {}; }', 'widget.cpp').language).toBe('cpp');
  });

  it('allows SQL inside Python strings and rejects JavaScript classes', () => {
    const pythonWithSql = 'query = "SELECT name FROM users"\ncursor.execute(query)';
    expect(detectLanguage(pythonWithSql, 'repository.py').language).toBe('python');
    expect(detectLanguage('class Widget { constructor() {} }').language).toBeNull();
  });

  it('rejects unsupported or ambiguous input', () => {
    expect(detectLanguage('SELECT * FROM users;', 'query.sql').language).toBeNull();
    expect(detectLanguage('SELECT * FROM users;', 'query.sql').error).toContain('Python');
    expect(detectLanguage('hello world').language).toBeNull();
    expect(detectLanguage('hello world').error).toBeTruthy();
  });
});

describe('source input safety', () => {
  it('rejects NUL, replacement characters and dangerous control text', () => {
    expect(validateSourceText("print('ok')\0")).toContain('NUL');
    expect(validateSourceText("print('ok')\uFFFD")).toContain('UTF-8');
    expect(validateSourceText("x=1\x01\x02")).toContain('控制字符');
    expect(validateSourceText("def ok():\n\treturn 1\n")).toBeNull();
  });

  it('uses slash, NFC and cross-platform casing for path identity', () => {
    expect(canonicalPathKey('Pkg\\cafe\u0301.py')).toBe(canonicalPathKey('pkg/caf\u00e9.py'));
    expect(duplicateCanonicalPaths(['Pkg\\item.py', 'pkg/item.py'])).toHaveLength(2);
  });

  it('chooses a non-conflicting virtual snippet name', () => {
    expect(uniqueSnippetFilename('python', ['SNIPPET.py', 'snippet-2.py'])).toBe('snippet-3.py');
    expect(uniqueSnippetFilename('cpp', ['src/main.cpp'])).toBe('snippet.cpp');
  });
});
