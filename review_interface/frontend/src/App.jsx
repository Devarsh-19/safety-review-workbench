import React, { useState } from 'react';
import LoginScreen from './components/LoginScreen';
import SessionQueue from './components/SessionQueue';
import SessionViewer from './components/SessionViewer';
import AudioSessionQueue from './components/AudioSessionQueue';
import AudioSessionViewer from './components/AudioSessionViewer';

export default function App() {
  const [reviewerName,     setReviewerName]     = useState('');
  const [reviewerRole,     setReviewerRole]     = useState('');
  const [workspace,        setWorkspace]        = useState('chat');   // 'chat' | 'audio'
  const [currentScreen,    setCurrentScreen]    = useState('queue');
  const [selectedSessionId,setSelectedSessionId]= useState(null);
  const [sessionList,      setSessionList]      = useState([]);

  if (!reviewerName) {
    return (
      <LoginScreen
        onLogin={(name, role, mode) => {
          setReviewerName(name);
          setReviewerRole(role);
          setWorkspace(mode || 'chat');
        }}
      />
    );
  }

  if (workspace === 'audio') {
    if (currentScreen === 'session') {
      return (
        <AudioSessionViewer
          sId={selectedSessionId}
          reviewerName={reviewerName}
          reviewerRole={reviewerRole}
          onBack={() => {
            setCurrentScreen('queue');
            setSelectedSessionId(null);
          }}
        />
      );
    }
    return (
      <AudioSessionQueue
        reviewerName={reviewerName}
        reviewerRole={reviewerRole}
        onSelectSession={(id) => {
          setSelectedSessionId(id);
          setCurrentScreen('session');
        }}
      />
    );
  }

  if (currentScreen === 'session') {
    return (
      <SessionViewer
        sessionId={selectedSessionId}
        sessionList={sessionList}
        reviewerName={reviewerName}
        reviewerRole={reviewerRole}
        onBack={() => {
          setCurrentScreen('queue');
          setSelectedSessionId(null);
        }}
        onNavigate={setSelectedSessionId}
      />
    );
  }

  return (
    <SessionQueue
      reviewerName={reviewerName}
      reviewerRole={reviewerRole}
      onSelectSession={(id, list) => {
        setSelectedSessionId(id);
        setSessionList(list || []);
        setCurrentScreen('session');
      }}
    />
  );
}
