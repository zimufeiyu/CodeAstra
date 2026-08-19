type ProductBrandProps = {
  compact?: boolean;
  subtitle?: string;
};

export function ProductBrand({ compact = false, subtitle }: ProductBrandProps) {
  return <div className={compact ? "product-brand product-brand-compact" : "product-brand"}>
    <span className="product-brand-mark" aria-hidden="true">✦</span>
    <span className="product-brand-copy">
      <strong>CodeAstra</strong>
      {subtitle ? <small>{subtitle}</small> : null}
    </span>
  </div>;
}
