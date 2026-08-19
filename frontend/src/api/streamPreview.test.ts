import { describe, expect, it } from "vitest";

import { formatStreamingReview } from "./streamPreview";

describe("streaming review preview", () => {
  it("turns partial review JSON into readable Chinese assistant text", () => {
    const raw =
      '{"summary":"正在检查边界条件","findings":[{' +
      '"title":"可能的空指针","evidence":"user.name",' +
      '"impact":"可能导致崩溃","suggestion":"访问前检查 user"';

    const preview = formatStreamingReview(raw);

    expect(preview).toContain("正在检查边界条件");
    expect(preview).toContain("可能的空指针");
    expect(preview).toContain("证据 · user.name");
    expect(preview).toContain("影响 · 可能导致崩溃");
    expect(preview).toContain("建议 · 访问前检查 user");
    expect(preview).not.toContain('{"summary"');
  });

  it("does not render identical repeated findings more than once", () => {
    const repeated =
      '{"summary":"检查完成","findings":[' +
      '{"title":"未使用的导入","evidence":"import os","impact":"增加冗余","suggestion":"删除导入"},' +
      '{"title":"未使用的导入","evidence":"import os","impact":"增加冗余","suggestion":"删除导入"}';

    const preview = formatStreamingReview(repeated);

    expect(preview.match(/问题 · 未使用的导入/g)).toHaveLength(1);
    expect(preview.match(/证据 · import os/g)).toHaveLength(1);
  });

});
