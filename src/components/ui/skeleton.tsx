import { cn } from "@/lib/utils";

function Skeleton({
  className,
  pulse = false,
  ...props
}: React.ComponentProps<"div"> & { pulse?: boolean }) {
  return (
    <div
      data-slot="skeleton"
      className={cn("rounded-md bg-accent/70", pulse && "animate-pulse", className)}
      {...props}
    />
  );
}

export { Skeleton };
