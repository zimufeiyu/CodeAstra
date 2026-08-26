type Props = {
  diff: string;
  label: string;
};

export function UnifiedDiff({ diff, label }: Props) {
  const lines = diff.replace(/\r\n?/g, "\n").split("\n");
  return (
    <pre aria-label={label} className="fix-diff-lines">
      <code>{lines.map((line, index) => (
        <span
          key={`${index}-${line}`}
          className={line.startsWith("+") && !line.startsWith("+++")
            ? "fix-diff-line added"
            : line.startsWith("-") && !line.startsWith("---")
              ? "fix-diff-line removed"
              : "fix-diff-line"}
        >{line || " "}</span>
      ))}</code>
    </pre>
  );
}
