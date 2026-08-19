import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ProductBrand } from "./ProductBrand";

describe("ProductBrand", () => {
  it("renders the shared star mark, CodeAstra name, and supplied subtitle", () => {
    render(<ProductBrand subtitle="星鉴" />);

    expect(screen.getByText("✦")).toHaveAttribute("aria-hidden", "true");
    expect(screen.getByText("CodeAstra")).toBeVisible();
    expect(screen.getByText("星鉴")).toBeVisible();
  });
});
