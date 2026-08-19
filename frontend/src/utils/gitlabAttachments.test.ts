import { describe, expect, test } from "vitest";

import {
  mergeLocalAttachments,
  remainingGitLabPaths,
  replaceGitLabAttachments,
} from "./gitlabAttachments";

type Attachment = {
  filename: string;
  source: "local" | "gitlab";
  content: string;
};

describe("GitLab attachment state", () => {
  test("replaces every previous GitLab file when another MR is imported", () => {
    const current: Attachment[] = [
      { filename: "local.py", source: "local", content: "local" },
      { filename: "mr-a.py", source: "gitlab", content: "a" },
      { filename: "shared.py", source: "gitlab", content: "old" },
    ];
    const additions: Attachment[] = [
      { filename: "shared.py", source: "gitlab", content: "new" },
      { filename: "mr-b.py", source: "gitlab", content: "b" },
    ];

    expect(replaceGitLabAttachments(current, additions)).toEqual([
      current[0],
      additions[0],
      additions[1],
    ]);
  });

  test("local files replace same-path GitLab files and remove their provenance", () => {
    const current: Attachment[] = [
      { filename: "src/app.py", source: "gitlab", content: "gitlab" },
      { filename: "src/keep.py", source: "gitlab", content: "keep" },
    ];
    const local: Attachment[] = [
      { filename: "src/app.py", source: "local", content: "local" },
    ];

    expect(mergeLocalAttachments(current, local)).toEqual([current[1], local[0]]);
    expect(
      remainingGitLabPaths(
        ["src/app.py", "src/keep.py"],
        local.map((item) => item.filename),
      ),
    ).toEqual(["src/keep.py"]);
  });
});
