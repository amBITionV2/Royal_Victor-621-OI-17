# Simulated diagnosis agent
class DiagnoseAgent:
    def analyze(self, log: str):
        if "CrashLoopBackOff" in log:
            return "PodFailure"
        elif "High CPU" in log:
            return "ScaleUp"
        elif "RegionDown" in log:
            return "RegionIsolation"
        else:
            return "UnknownIssue"
