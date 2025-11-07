import React from 'react';
import Navbar from './Navbar';

const MainLayout = ({ currentTab, onTabChange, children }) => {
  return (
    <div className="min-h-screen bg-gray-900 text-white">
      <Navbar currentTab={currentTab} onTabChange={onTabChange} />
      <div className="p-6">
        {children}
      </div>
    </div>
  );
};

export default MainLayout;