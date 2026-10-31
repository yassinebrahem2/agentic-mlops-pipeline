import { ReportDetails } from "@/components/ReportDetails";
import { QueryStatus } from "@/components/QueryStatus";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { useReport } from "./useReport";

interface ReportDialogProps {
  projectId: string;
  reportId: string;
  model: string;
  onClose: () => void;
}

export function ReportDialog({ projectId, reportId, model, onClose }: ReportDialogProps) {
  const query = useReport(projectId, reportId);

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-2xl">
        <DialogTitle>Report for {model}</DialogTitle>
        <DialogDescription>
          The report behind this decision, as the promotion approval showed it.
        </DialogDescription>
        <QueryStatus query={query} loading="Loading report..." />
        {query.data && <ReportDetails report={query.data} />}
      </DialogContent>
    </Dialog>
  );
}
