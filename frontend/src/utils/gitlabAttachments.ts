export type SourcedAttachment = {
  filename: string;
  source?: "local" | "gitlab" | "local-diff";
};

export function mergeLocalAttachments<T extends SourcedAttachment>(
  current: T[],
  additions: T[],
): T[] {
  const addedPaths = new Set(additions.map((item) => item.filename));
  return [...current.filter((item) => !addedPaths.has(item.filename)), ...additions];
}

export function replaceGitLabAttachments<T extends SourcedAttachment>(
  current: T[],
  additions: T[],
): T[] {
  const addedPaths = new Set(additions.map((item) => item.filename));
  return [
    ...current.filter(
      (item) => item.source !== "gitlab" && !addedPaths.has(item.filename),
    ),
    ...additions,
  ];
}

export function replaceLocalDiffAttachments<T extends SourcedAttachment>(
  current: T[],
  additions: T[],
): T[] {
  const addedPaths = new Set(additions.map((item) => item.filename));
  return [
    ...current.filter(
      (item) => item.source !== "local-diff" && !addedPaths.has(item.filename),
    ),
    ...additions,
  ];
}

export function remainingGitLabPaths(
  selectedPaths: string[],
  replacedPaths: Iterable<string>,
): string[] {
  const replaced = new Set(replacedPaths);
  return selectedPaths.filter((path) => !replaced.has(path));
}
