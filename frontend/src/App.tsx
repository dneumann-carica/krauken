import { BrowserRouter, Route, Routes } from "react-router-dom";
import { DevPanelView } from "./views/DevPanel/DevPanelView";
import { GettingStartedView } from "./views/GettingStarted/GettingStartedView";
import { HardwareSetupView } from "./views/HardwareSetup/HardwareSetupView";

function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<GettingStartedView />} />
        <Route path="/hardware" element={<HardwareSetupView />} />
        <Route path="/dev" element={<DevPanelView />} />
      </Routes>
    </BrowserRouter>
  );
}

export default App;
