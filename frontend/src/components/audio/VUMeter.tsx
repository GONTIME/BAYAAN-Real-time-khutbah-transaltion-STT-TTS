interface VUMeterProps {
  level: number; // 0..1
}

export function VUMeter({ level }: VUMeterProps) {
  // Determine color based on level thresholds
  const colorClass = level > 0.8 ? "peak" : level > 0.4 ? "mid" : "low";
  const width = Math.min(100, level * 100);

  return (
    <div className="vu-meter-container">
      <div className="vu-label">
        <span>Input Level:</span>
        <span>{Math.round(width)}%</span>
      </div>
      <div className="vu-meter-track">
        <div 
          className={`vu-meter-fill ${colorClass}`} 
          style={{ width: `${width}%` }} 
        />
      </div>
    </div>
  );
}
