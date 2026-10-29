import type { components } from "@/api/schema";

export type CommandSpec = components["schemas"]["CommandSpec"];

export function CommandsTable({ commands }: { commands: CommandSpec[] }) {
  if (commands.length === 0) {
    return <span className="text-xs text-muted-foreground">none</span>;
  }
  return (
    <div className="overflow-x-auto rounded border border-border/60 bg-muted/20">
      <table className="w-full text-left text-xs">
        <thead className="border-b border-border/60 bg-muted/40 font-semibold text-foreground">
          <tr>
            <th className="p-1.5">Kind</th>
            <th className="p-1.5">Type</th>
            <th className="p-1.5">Template / Workflow</th>
            <th className="p-1.5">Inputs</th>
            <th className="p-1.5">Placeholders</th>
            <th className="p-1.5">Deploys</th>
            <th className="p-1.5">Entry Files</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border/40 text-muted-foreground">
          {commands.map((cmd, idx) => (
            <tr key={`${cmd.kind}-${cmd.type}-${String(idx)}`}>
              <td className="p-1.5 font-medium text-foreground">{cmd.kind}</td>
              <td className="p-1.5">{cmd.type}</td>
              <td className="p-1.5 font-mono text-[11px] text-foreground">
                {cmd.template ?? cmd.workflow ?? "none"}
              </td>
              <td className="p-1.5">
                {Object.keys(cmd.inputs).length === 0
                  ? "none"
                  : Object.entries(cmd.inputs)
                      .map(([k, v]) => `${k}=${v}`)
                      .join(", ")}
              </td>
              <td className="p-1.5">
                {cmd.placeholders.length === 0 ? "none" : cmd.placeholders.join(", ")}
              </td>
              <td className="p-1.5">{cmd.deploys ? "yes" : "no"}</td>
              <td className="p-1.5">
                {cmd.entry_files.length === 0 ? "none" : cmd.entry_files.join(", ")}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
