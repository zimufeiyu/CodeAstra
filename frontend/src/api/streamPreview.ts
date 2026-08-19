type PreviewField = "summary" | "title" | "evidence" | "impact" | "suggestion";

const labels: Record<Exclude<PreviewField, "summary">, string> = {
  title: "问题",
  evidence: "证据",
  impact: "影响",
  suggestion: "建议",
};

function decodePartialJsonString(value: string): string {
  const safe = value.endsWith("\\") ? value.slice(0, -1) : value;
  try {
    return JSON.parse(`"${safe}"`) as string;
  } catch {
    return safe
      .replace(/\\n/g, "\n")
      .replace(/\\t/g, " ")
      .replace(/\\"/g, '"')
      .replace(/\\\\/g, "\\");
  }
}

export function formatStreamingReview(source: string): string {
  const fieldPattern = /"(summary|title|evidence|impact|suggestion)"\s*:\s*"/g;
  const sections: string[] = [];
  const seenSections = new Set<string>();
  let match: RegExpExecArray | null;

  while ((match = fieldPattern.exec(source)) !== null) {
    const field = match[1] as PreviewField;
    let cursor = fieldPattern.lastIndex;
    let escaped = false;
    let encoded = "";

    while (cursor < source.length) {
      const character = source[cursor];
      if (!escaped && character === '"') break;
      encoded += character;
      if (character === "\\" && !escaped) {
        escaped = true;
      } else {
        escaped = false;
      }
      cursor += 1;
    }

    const text = decodePartialJsonString(encoded).trim();
    if (!text) continue;
    const section = field === "summary" ? text : `${labels[field]} · ${text}`;
    if (seenSections.has(section)) continue;
    seenSections.add(section);
    sections.push(section);
  }

  return sections.join("\n\n");
}
