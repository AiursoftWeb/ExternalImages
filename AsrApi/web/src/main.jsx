import { useEffect, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import '@fontsource/roboto/400.css'
import '@fontsource/roboto/500.css'
import '@fontsource/roboto/700.css'
import CloudUploadOutlinedIcon from '@mui/icons-material/CloudUploadOutlined'
import DescriptionOutlinedIcon from '@mui/icons-material/DescriptionOutlined'
import ExpandMoreIcon from '@mui/icons-material/ExpandMore'
import FileDownloadOutlinedIcon from '@mui/icons-material/FileDownloadOutlined'
import GraphicEqOutlinedIcon from '@mui/icons-material/GraphicEqOutlined'
import MicRoundedIcon from '@mui/icons-material/MicRounded'
import PlayArrowRoundedIcon from '@mui/icons-material/PlayArrowRounded'
import StopRoundedIcon from '@mui/icons-material/StopRounded'
import {
  Alert,
  Accordion,
  AccordionDetails,
  AccordionSummary,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  Container,
  CssBaseline,
  Divider,
  FormControl,
  FormHelperText,
  InputLabel,
  LinearProgress,
  MenuItem,
  Paper,
  Select,
  Stack,
  TextField,
  ThemeProvider,
  Typography,
  createTheme,
} from '@mui/material'

const theme = createTheme({
  palette: {
    primary: { main: '#4058c7' },
    secondary: { main: '#5b63d9' },
    background: { default: '#f7f8ff', paper: '#ffffff' },
  },
  shape: { borderRadius: 16 },
  typography: { fontFamily: 'Roboto, Arial, sans-serif' },
})

const taskStorageKey = 'asr-api-demo-tasks'
const maxAudioFileSize = 1024 * 1024 * 1024

function loadTasks() {
  try {
    const storedTasks = JSON.parse(localStorage.getItem(taskStorageKey) || '[]')
    if (!Array.isArray(storedTasks)) return []
    return storedTasks.map((task) => task.status === 'processing'
      ? { ...task, status: 'interrupted', error: '页面刷新或关闭，无法继续跟踪该任务。' }
      : task)
  } catch (error) {
    console.error('Failed to load local task history:', error)
    return []
  }
}

function taskStatusLabel(status) {
  if (status === 'processing') return '处理中'
  if (status === 'completed') return '已完成'
  if (status === 'cancelled') return '已取消'
  if (status === 'interrupted') return '已中断'
  return '失败'
}

function taskStatusColor(status) {
  if (status === 'processing') return 'primary'
  if (status === 'completed') return 'success'
  if (status === 'cancelled') return 'default'
  return 'error'
}

function generateClientTaskId() {
  const randomPart = crypto.randomUUID?.() || Math.random().toString(36).slice(2)
  return `task_${Date.now()}_${randomPart}`
}

function defaultRealtimeUrl() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${window.location.hostname}:10095`
}

function parseRealtimeMessage(data) {
  const message = JSON.parse(data)
  if (message === null || Array.isArray(message) || typeof message !== 'object') {
    throw new TypeError('Realtime message must be an object')
  }
  if ('is_final' in message && typeof message.is_final !== 'boolean') {
    throw new TypeError('Realtime message is_final must be a boolean')
  }
  if ('text' in message && typeof message.text !== 'string') {
    throw new TypeError('Realtime message text must be a string')
  }
  return message
}

function resampleAudio(samples, sourceRate, targetRate) {
  if (sourceRate === targetRate) return samples
  const outputLength = Math.round(samples.length * targetRate / sourceRate)
  const output = new Float32Array(outputLength)
  const ratio = sourceRate / targetRate
  for (let index = 0; index < outputLength; index += 1) {
    const sourceIndex = index * ratio
    const before = Math.floor(sourceIndex)
    const after = Math.min(before + 1, samples.length - 1)
    const blend = sourceIndex - before
    output[index] = samples[before] * (1 - blend) + samples[after] * blend
  }
  return output
}

function encodePcm(samples) {
  const output = new Int16Array(samples.length)
  for (let index = 0; index < samples.length; index += 1) {
    output[index] = Math.max(-1, Math.min(1, samples[index])) * 0x7fff
  }
  return output.buffer
}

async function startMicrophone(onAudio) {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1 } })
  const audioContext = new AudioContext({ sampleRate: 16000 })
  const source = audioContext.createMediaStreamSource(stream)
  const processor = audioContext.createScriptProcessor(1024, 1, 1)
  const silentGain = audioContext.createGain()
  silentGain.gain.value = 0
  processor.onaudioprocess = (event) => {
    const samples = event.inputBuffer.getChannelData(0)
    const pcm = encodePcm(resampleAudio(samples, audioContext.sampleRate, 16000))
    onAudio(pcm)
  }
  source.connect(processor)
  processor.connect(silentGain)
  silentGain.connect(audioContext.destination)

  return () => {
    processor.disconnect()
    silentGain.disconnect()
    source.disconnect()
    stream.getTracks().forEach((track) => track.stop())
    audioContext.close()
  }
}

function App() {
  const [token, setToken] = useState('')
  const [model, setModel] = useState('funasr')
  const [config, setConfig] = useState({ whisperx: true, funasr: true, funasrrealtime: true, whisperx_single_model: true })
  const [modelOptions, setModelOptions] = useState([])
  const [loadingModels, setLoadingModels] = useState(false)
  const [modelOptionsError, setModelOptionsError] = useState('')
  const [level, setLevel] = useState('')
  const [language, setLanguage] = useState('')
  const [file, setFile] = useState(null)
  const [error, setError] = useState('')
  const [tasks, setTasks] = useState(loadTasks)
  const [selectedTaskId, setSelectedTaskId] = useState(null)
  const [storageError, setStorageError] = useState('')
  const [realtimeUrl, setRealtimeUrl] = useState(defaultRealtimeUrl)
  const [realtimeToken, setRealtimeToken] = useState('')
  const [realtimeStatus, setRealtimeStatus] = useState('未连接')
  const [realtimePartial, setRealtimePartial] = useState('')
  const [realtimeFinal, setRealtimeFinal] = useState('')
  const [realtimeError, setRealtimeError] = useState('')
  const microphoneRef = useRef(null)
  const socketRef = useRef(null)
  const stoppingRealtimeRef = useRef(false)
  const realtimeConnectedRef = useRef(false)
  const taskControllersRef = useRef(new Map())

  const [configLoaded, setConfigLoaded] = useState(false)

  useEffect(() => {
    fetch('/config')
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`)
        return response.json()
      })
      .then((data) => {
        if (data) {
          setConfig(data)
          if (data.funasr === false && data.whisperx === true) {
            setModel('whisperx')
          } else if (data.whisperx === false && data.funasr === true) {
            setModel('funasr')
          }
        }
      })
      .catch((err) => console.error('加载系统配置失败：', err))
      .finally(() => setConfigLoaded(true))
  }, [])

  useEffect(() => {
    if (!token) {
      setModelOptions([])
      setModelOptionsError('')
      return
    }
    setLoadingModels(true)
    setModelOptionsError('')
    fetch('/v1/models', { headers: { Authorization: `Bearer ${token}` } })
      .then((response) => (response.ok ? response.json() : Promise.reject(new Error(`HTTP ${response.status}`))))
      .then((data) => { if (data?.data) setModelOptions(data.data) })
      .catch(() => setModelOptionsError('模型档位加载失败，请确认 Token 与上游服务状态。'))
      .finally(() => setLoadingModels(false))
  }, [token])

  useEffect(() => {
    try {
      localStorage.setItem(taskStorageKey, JSON.stringify(tasks))
      setStorageError('')
    } catch (storageWriteError) {
      console.error('Failed to save local task history:', storageWriteError)
      setStorageError('任务结果仍可查看，但浏览器本地缓存空间不足。')
    }
  }, [tasks])

  useEffect(() => {
    if (tasks.length === 0) {
      setSelectedTaskId(null)
      return
    }
    if (!tasks.some((task) => task.id === selectedTaskId)) {
      setSelectedTaskId(tasks[0].id)
    }
  }, [tasks, selectedTaskId])

  function updateTask(taskId, changes) {
    setTasks((currentTasks) => currentTasks.map((task) => (
      task.id === taskId ? { ...task, ...changes } : task
    )))
  }

  async function handleCancelTask(taskId) {
    try {
      const response = await fetch(`/v1/tasks/${taskId}/cancel`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}` }
      })
      if (response.ok) {
        updateTask(taskId, { status: 'cancelled', error: '任务已取消。' })
        taskControllersRef.current.get(taskId)?.abort()
      } else {
        const body = await response.json()
        alert(`取消失败：${body.error || '未知错误'}`)
      }
    } catch (err) {
      alert(`无法连接服务：${err.message}`)
    }
  }

  function removeTask(taskId) {
    setTasks((currentTasks) => currentTasks.filter((task) => task.id !== taskId))
  }

  function clearTasks() {
    setTasks([])
  }

  const levelOptions = modelOptions.filter((option) => option.owned_by === model)
  const isSingleModel = levelOptions.length === 1

  useEffect(() => {
    if (isSingleModel) {
      setLevel(levelOptions[0].id)
    } else {
      setLevel('')
    }
  }, [model, modelOptions, isSingleModel])

  const whisperxBakedList = modelOptions
    .filter((option) => option.owned_by === 'whisperx' && option.baked !== false)
    .map((option) => option.id)
  const whisperxBakedStr = config.whisperx_single_model
    ? 'large-v3'
    : (whisperxBakedList.length > 0 ? whisperxBakedList.join(' / ') : 'small / medium / large-v3')

  const isModelReadOnly = configLoaded && ((config.whisperx && !config.funasr) || (!config.whisperx && config.funasr))
  const processingTaskCount = tasks.filter((task) => task.status === 'processing').length
  const selectedTask = tasks.find((task) => task.id === selectedTaskId)
  const selectedResult = selectedTask?.status === 'completed'
    ? selectedTask.result
    : selectedTask?.status === 'processing'
      ? '识别处理中…'
      : selectedTask?.error || '请选择任务查看识别结果。'

  async function transcribe() {
    if (!file || !token) {
      setError('请选择音频文件并填写 API Token。')
      return
    }
    if (file.size > maxAudioFileSize) {
      setError('单个音频文件不能超过 1 GiB。长音频请先由调用方分片。')
      return
    }

    setError('')
    const taskId = generateClientTaskId()
    const controller = new AbortController()
    const startedAt = performance.now()
    const task = {
      id: taskId,
      status: 'processing',
      filename: file.name,
      model,
      level,
      created_at: new Date().toISOString(),
      result: '',
      error: '',
      duration_ms: null,
    }
    setTasks((currentTasks) => [task, ...currentTasks])
    setSelectedTaskId(taskId)
    taskControllersRef.current.set(taskId, controller)

    const formData = new FormData()
    formData.append('file', file)
    formData.append('model', model)
    if (level) formData.append('level', level)
    if (language) formData.append('language', language)

    try {
      const response = await fetch('/v1/audio/transcriptions', {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${token}`,
          'X-Task-Id': taskId,
        },
        body: formData,
        signal: controller.signal,
      })
      const body = await response.text()
      const durationMs = performance.now() - startedAt
      if (!response.ok) {
        updateTask(taskId, {
          status: 'failed',
          error: readError(body) || `请求失败（HTTP ${response.status}）`,
          duration_ms: durationMs,
        })
        return
      }
      updateTask(taskId, {
        status: 'completed',
        result: readText(body) || body,
        duration_ms: durationMs,
      })
    } catch (requestError) {
      if (requestError.name !== 'AbortError') {
        updateTask(taskId, {
          status: 'failed',
          error: requestError.message || '无法连接到服务。',
          duration_ms: performance.now() - startedAt,
        })
      }
    } finally {
      taskControllersRef.current.delete(taskId)
    }
  }

  async function startRealtime() {
    if (!realtimeToken) {
      setRealtimeError('请填写实时服务 Token。')
      return
    }
    if (!navigator.mediaDevices?.getUserMedia) {
      setRealtimeError('当前浏览器不支持麦克风采集。')
      return
    }

    setRealtimeError('')
    setRealtimePartial('')
    setRealtimeFinal('')
    setRealtimeStatus('连接中…')
    stoppingRealtimeRef.current = false
    realtimeConnectedRef.current = false
    let socket
    try {
      socket = new WebSocket(realtimeUrl, ['binary', `bearer.${realtimeToken}`])
    } catch (connectionError) {
      setRealtimeError(connectionError.message || '实时服务地址无效。')
      setRealtimeStatus('未连接')
      return
    }
    socket.binaryType = 'arraybuffer'
    socketRef.current = socket

    socket.onopen = async () => {
      realtimeConnectedRef.current = true
      socket.send(JSON.stringify({
        mode: '2pass',
        audio_fs: 16000,
        chunk_size: [5, 10, 5],
        chunk_interval: 10,
        wav_name: 'microphone',
        is_speaking: true,
      }))
      try {
        const stopMicrophone = await startMicrophone((pcm) => {
          if (socket.readyState === WebSocket.OPEN) socket.send(pcm)
        })
        if (socketRef.current !== socket || socket.readyState !== WebSocket.OPEN) {
          stopMicrophone()
          return
        }
        microphoneRef.current = stopMicrophone
        setRealtimeStatus('正在实时识别')
      } catch (microphoneError) {
        stoppingRealtimeRef.current = true
        socket.close()
        setRealtimeError(microphoneError.message || '无法访问麦克风。')
      }
    }

    socket.onmessage = (event) => {
      let message
      try {
        message = parseRealtimeMessage(event.data)
      } catch {
        stoppingRealtimeRef.current = true
        setRealtimeError('实时服务返回了无效的 JSON 消息。')
        socket.close(1003, 'Invalid JSON response')
        return
      }
      if (message.is_final) {
        setRealtimeFinal(message.text || '')
        stoppingRealtimeRef.current = true
        socket.close()
        return
      }
      setRealtimePartial(message.text || '')
    }

    socket.onerror = () => {
      setRealtimeError('无法连接实时识别服务。请检查服务地址和 Token。')
    }

    socket.onclose = (event) => {
      microphoneRef.current?.()
      microphoneRef.current = null
      socketRef.current = null
      setRealtimeStatus('未连接')
      if (!stoppingRealtimeRef.current) {
        if (!realtimeConnectedRef.current) {
          setRealtimeError('无法连接实时识别服务，请确认服务已启动、地址与 Token 正确。')
        } else {
          setRealtimeError(`实时服务已断开连接（code ${event.code}${event.reason ? '：' + event.reason : ''}）`)
        }
      }
    }
  }

  function stopRealtime() {
    const socket = socketRef.current
    if (socket?.readyState === WebSocket.OPEN) {
      stoppingRealtimeRef.current = true
      socket.send(JSON.stringify({ is_speaking: false }))
    }
    microphoneRef.current?.()
    microphoneRef.current = null
    setRealtimeStatus('正在结束…')
  }

  function downloadResult() {
    if (!selectedTask?.result?.trim()) return
    const url = URL.createObjectURL(new Blob([selectedTask.result], { type: 'text/plain;charset=utf-8' }))
    const link = document.createElement('a')
    link.href = url
    link.download = `${selectedTask.filename}.txt`
    document.body.appendChild(link)
    link.click()
    link.remove()
    URL.revokeObjectURL(url)
  }

  return (
    <ThemeProvider theme={theme}>
      <CssBaseline />
      <Box sx={{ minHeight: '100vh', py: { xs: 3, md: 7 }, background: 'radial-gradient(circle at top right, #e2e7ff 0, transparent 33%), #f7f8ff' }}>
        <Container maxWidth="md">
          <Stack spacing={4}>
            <Box>
              <Chip label="ASR API" color="primary" size="small" sx={{ mb: 2, fontWeight: 700 }} />
              <Typography variant="h3" component="h1" sx={{ fontWeight: 700, letterSpacing: '-0.04em' }}>
                Speech to text, simply.
              </Typography>
              <Typography color="text.secondary" sx={{ mt: 1, maxWidth: 580 }}>
                {config.funasr && config.whisperx
                  ? '上传音频，使用已部署的 WhisperX 或 FunASR 模型快速体验语音识别。'
                  : config.whisperx
                  ? '上传音频，使用已部署的 WhisperX 模型快速体验语音识别。'
                  : '上传音频，使用已部署的 FunASR 模型快速体验语音识别。'}
              </Typography>
            </Box>

            <Card elevation={0} sx={{ border: '1px solid', borderColor: 'divider' }}>
              <CardContent sx={{ p: { xs: 3, sm: 4 } }}>
                <Stack spacing={3}>
                  <Stack direction="row" spacing={1.5} alignItems="center">
                    <GraphicEqOutlinedIcon color="primary" />
                    <Typography variant="h6" fontWeight={700}>开始识别</Typography>
                  </Stack>
                  <TextField label="API Token" type="password" value={token} onChange={(event) => setToken(event.target.value)} fullWidth autoComplete="off" helperText="仅用于本次请求，不会被保存。" />
                  {!configLoaded ? (
                    <Box sx={{ py: 2 }}>
                      <LinearProgress />
                      <Typography variant="body2" color="text.secondary" sx={{ mt: 1, textAlign: 'center' }}>
                        正在加载系统配置…
                      </Typography>
                    </Box>
                  ) : (
                    <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2}>
                      {isModelReadOnly ? (
                        <TextField
                          label="识别模型"
                          value={model === 'whisperx' ? 'WhisperX' : 'FunASR'}
                          InputProps={{ readOnly: true }}
                          fullWidth
                        />
                      ) : (
                        <FormControl fullWidth>
                          <InputLabel id="model-label">识别模型</InputLabel>
                          <Select labelId="model-label" label="识别模型" value={model} onChange={(event) => { setModel(event.target.value); setLevel('') }}>
                            {config.funasr && <MenuItem value="funasr">FunASR</MenuItem>}
                            {config.whisperx && <MenuItem value="whisperx">WhisperX</MenuItem>}
                          </Select>
                        </FormControl>
                      )}

                      {model === 'whisperx' && config.whisperx_single_model ? (
                        <TextField
                          label="模型档位"
                          value="large-v3"
                          helperText="单模型模式已启用，模型已固定"
                          InputProps={{ readOnly: true }}
                          fullWidth
                        />
                      ) : (
                        <FormControl fullWidth error={Boolean(modelOptionsError)}>
                          <InputLabel id="level-label" shrink>模型档位</InputLabel>
                          <Select labelId="level-label" label="模型档位" value={level} displayEmpty disabled={!token || loadingModels || isSingleModel} onChange={(event) => setLevel(event.target.value)}>
                            {isSingleModel ? (
                              <MenuItem value={levelOptions[0].id}>{levelOptions[0].id}</MenuItem>
                            ) : (
                              <>
                                <MenuItem value=""><em>默认（{model} 默认档）</em></MenuItem>
                                {levelOptions.map((option) => (
                                  <MenuItem key={option.id} value={option.id}>
                                    {option.id}
                                  </MenuItem>
                                ))}
                              </>
                            )}
                          </Select>
                          {!token
                            ? <FormHelperText>请先填写 API Token 以加载可用模型档位</FormHelperText>
                            : loadingModels
                              ? <FormHelperText>正在加载模型档位…</FormHelperText>
                              : modelOptionsError
                                ? <FormHelperText error>{modelOptionsError}</FormHelperText>
                                : isSingleModel
                                  ? <FormHelperText>单模型模式已启用，无法修改档位</FormHelperText>
                                  : null}
                        </FormControl>
                      )}
                    </Stack>
                  )}
                  <TextField label="语言（可选）" value={language} onChange={(event) => setLanguage(event.target.value)} fullWidth placeholder="例如 zh、en" />
                  <Button component="label" variant="outlined" color="primary" startIcon={<CloudUploadOutlinedIcon />} sx={{ minHeight: 88, borderStyle: 'dashed', justifyContent: 'flex-start', px: 3, textTransform: 'none' }}>
                    <Box textAlign="left">
                      <Typography fontWeight={600}>{file ? file.name : '选择音频文件'}</Typography>
                      <Typography variant="body2" color="text.secondary">支持服务端接受的音频格式，单个音频文件上限 1 GiB；长音频请由调用方自行分片</Typography>
                    </Box>
                    <input hidden type="file" accept="audio/*" onChange={(event) => setFile(event.target.files?.[0] || null)} />
                  </Button>
                  {error && <Alert severity="error">{error}</Alert>}
                  <Button variant="contained" size="large" onClick={transcribe} startIcon={<PlayArrowRoundedIcon />} sx={{ alignSelf: 'flex-start', px: 3 }}>
                    开始识别
                  </Button>
                  {processingTaskCount > 0 && <LinearProgress />}
                </Stack>
              </CardContent>
            </Card>

            {storageError && <Alert severity="warning">{storageError}</Alert>}

            {tasks.length > 0 && (
              <Card elevation={0} sx={{ border: '1px solid', borderColor: 'divider' }}>
                <CardContent sx={{ p: { xs: 3, sm: 4 } }}>
                  <Stack spacing={3}>
                    <Stack direction="row" spacing={1.5} alignItems="center">
                      <GraphicEqOutlinedIcon color="primary" />
                      <Typography variant="h6" fontWeight={700}>本地任务记录</Typography>
                      <Chip label={`处理中: ${processingTaskCount}`} size="small" color="primary" />
                      <Button size="small" color="error" disabled={processingTaskCount > 0} onClick={clearTasks} sx={{ ml: 'auto' }}>
                        清空记录
                      </Button>
                    </Stack>
                    <Divider />
                    <Stack spacing={2}>
                      {tasks.map((task) => (
                        <Paper
                          key={task.id}
                          variant="outlined"
                          role="button"
                          tabIndex={0}
                          onClick={() => setSelectedTaskId(task.id)}
                          onKeyDown={(event) => {
                            if (event.key === 'Enter' || event.key === ' ') setSelectedTaskId(task.id)
                          }}
                          sx={{
                            p: 2,
                            display: 'flex',
                            alignItems: 'center',
                            justifyContent: 'space-between',
                            flexWrap: 'wrap',
                            gap: 2,
                            cursor: 'pointer',
                            borderColor: task.id === selectedTaskId ? 'primary.main' : 'divider',
                            bgcolor: task.id === selectedTaskId ? 'action.selected' : 'background.paper',
                          }}
                        >
                          <Box>
                            <Typography variant="subtitle2" fontWeight={600}>{task.filename}</Typography>
                            <Typography variant="caption" color="text.secondary" display="block">
                              ID: {task.id} | 模型: {task.model} ({task.level || '默认'})
                            </Typography>
                            <Typography variant="caption" color="text.secondary">
                              创建时间: {new Date(task.created_at).toLocaleString()}
                            </Typography>
                          </Box>
                          <Stack direction="row" spacing={2} alignItems="center">
                            <Chip
                              label={taskStatusLabel(task.status)}
                              size="small"
                              color={taskStatusColor(task.status)}
                            />
                            {task.status === 'processing' ? (
                              <Button size="small" color="error" variant="outlined" onClick={(event) => { event.stopPropagation(); handleCancelTask(task.id) }}>
                                取消
                              </Button>
                            ) : (
                              <Button size="small" color="error" onClick={(event) => { event.stopPropagation(); removeTask(task.id) }}>
                                删除
                              </Button>
                            )}
                          </Stack>
                        </Paper>
                      ))}
                    </Stack>
                  </Stack>
                </CardContent>
              </Card>
            )}

            <Paper elevation={0} sx={{ p: { xs: 3, sm: 4 }, border: '1px solid', borderColor: 'divider' }}>
              <Stack direction="row" spacing={1.5} alignItems="center" sx={{ mb: 2 }}>
                <DescriptionOutlinedIcon color="primary" />
                <Typography variant="h6" fontWeight={700}>识别结果</Typography>
                {selectedTask && <Typography variant="caption" color="text.secondary">{selectedTask.filename}</Typography>}
                <Button size="small" onClick={downloadResult} disabled={!selectedTask?.result?.trim()} startIcon={<FileDownloadOutlinedIcon />} sx={{ ml: 'auto' }}>
                  下载 TXT
                </Button>
              </Stack>
              <Divider sx={{ mb: 2 }} />
              <Typography component="pre" sx={{ m: 0, minHeight: 88, whiteSpace: 'pre-wrap', fontFamily: 'inherit', color: selectedTask?.status === 'completed' ? 'text.primary' : 'text.secondary' }}>
                {selectedResult || '识别结果为空。'}
              </Typography>
              {selectedTask?.duration_ms !== null && selectedTask?.duration_ms !== undefined && (
                <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
                  耗时：{(selectedTask.duration_ms / 1000).toFixed(2)} 秒
                </Typography>
              )}
            </Paper>

            {config.funasrrealtime && (
              <Card elevation={0} sx={{ border: '1px solid', borderColor: 'divider' }}>
                <CardContent sx={{ p: { xs: 3, sm: 4 } }}>
                  <Stack spacing={3}>
                    <Stack direction="row" spacing={1.5} alignItems="center">
                      <MicRoundedIcon color="primary" />
                      <Typography variant="h6" fontWeight={700}>实时麦克风识别</Typography>
                      <Chip label={realtimeStatus} size="small" color={realtimeStatus === '正在实时识别' ? 'success' : 'default'} />
                    </Stack>
                    <TextField label="实时服务地址" value={realtimeUrl} onChange={(event) => setRealtimeUrl(event.target.value)} fullWidth helperText="默认连接当前主机的 10095 端口。需要部署 funasr-realtime。" />
                    <TextField label="实时服务 Token" type="password" value={realtimeToken} onChange={(event) => setRealtimeToken(event.target.value)} fullWidth autoComplete="off" helperText="仅用于连接 funasr-realtime，不会被保存。" />
                    {realtimeError && <Alert severity="error">{realtimeError}</Alert>}
                    <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2}>
                      <Button variant="contained" onClick={startRealtime} disabled={realtimeStatus !== '未连接'} startIcon={<MicRoundedIcon />}>
                        连接麦克风
                      </Button>
                      <Button variant="outlined" color="error" onClick={stopRealtime} disabled={realtimeStatus !== '正在实时识别'} startIcon={<StopRoundedIcon />}>
                        停止识别
                      </Button>
                    </Stack>
                    <Paper variant="outlined" sx={{ p: 2, bgcolor: 'grey.50' }}>
                      <Typography variant="overline" color="text.secondary">增量文本</Typography>
                      <Typography sx={{ minHeight: 28, whiteSpace: 'pre-wrap' }}>{realtimePartial || '等待语音输入…'}</Typography>
                      <Divider sx={{ my: 1.5 }} />
                      <Typography variant="overline" color="text.secondary">最终文本</Typography>
                      <Typography sx={{ minHeight: 28, whiteSpace: 'pre-wrap' }}>{realtimeFinal || '句末校正结果会显示在这里。'}</Typography>
                    </Paper>
                  </Stack>
                </CardContent>
              </Card>
            )}

            <Paper elevation={0} sx={{ p: { xs: 3, sm: 4 }, border: '1px solid', borderColor: 'divider' }}>
              <Stack direction="row" spacing={1.5} alignItems="center" sx={{ mb: 2 }}>
                <DescriptionOutlinedIcon color="primary" />
                <Typography variant="h6" fontWeight={700}>API 接口说明</Typography>
              </Stack>
              <Stack spacing={1}>
                <ApiEndpoint method="GET" path="/healthz" description="检查网关是否正在运行，无需认证。" example="curl http://localhost:8080/healthz" />
                {config.funasrrealtime && (
                  <ApiEndpoint method="WS" path="ws://<host>:10095" description="连接 funasr-realtime，先发送配置 JSON，再连续发送 16 kHz、单声道、16-bit PCM 音频帧。" example={'Authorization: Bearer <ASR_REALTIME_TOKEN>\nSec-WebSocket-Protocol: binary\n\n{"mode":"2pass","audio_fs":16000,"chunk_size":[5,10,5],"chunk_interval":10,"is_speaking":true}\n\n{"is_speaking":false}'} />
                )}
                <ApiEndpoint method="GET" path="/v1/models" description="获取可用的语音识别模型列表。" example={'curl http://localhost:8080/v1/models \\\n  -H "Authorization: Bearer <ASR_API_TOKEN>"'} />
                <ApiEndpoint method="GET" path="/v1/system" description="获取网关及上游模型服务的运行状态。" example={'curl http://localhost:8080/v1/system \\\n  -H "Authorization: Bearer <ASR_API_TOKEN>"'} />
                <ApiEndpoint method="POST" path="/v1/tasks/:id/cancel" description="取消指定的排队中或运行中的语音识别任务。" example={'curl -X POST http://localhost:8080/v1/tasks/task_123456_000000/cancel \\\n  -H "Authorization: Bearer <ASR_API_TOKEN>"'} />
                <ApiEndpoint method="POST" path="/v1/audio/transcriptions" description="上传不超过 1 GiB 的音频并使用指定模型与档位返回转写结果。长音频的分片与结果合并由调用方负责；level 可选，省略时使用该引擎默认档。" example={
                  (config.funasr && config.whisperx) ?
                  `# FunASR：默认档 sensevoice，可指定 paraformer / paraformer-en\ncurl http://localhost:8080/v1/audio/transcriptions \\\n  -H "Authorization: Bearer <ASR_API_TOKEN>" \\\n  -F file=@meeting.wav \\\n  -F model=funasr \\\n  -F level=paraformer\n\n# WhisperX：默认档 large-v3，已烘焙 ${whisperxBakedStr}\ncurl http://localhost:8080/v1/audio/transcriptions \\\n  -H "Authorization: Bearer <ASR_API_TOKEN>" \\\n  -F file=@meeting.wav \\\n  -F model=whisperx \\\n  -F level=large-v3\n\n# 省略 -F level 即使用引擎默认档`
                  : config.funasr ?
                  `# FunASR：默认档 sensevoice，可指定 paraformer / paraformer-en\ncurl http://localhost:8080/v1/audio/transcriptions \\\n  -H "Authorization: Bearer <ASR_API_TOKEN>" \\\n  -F file=@meeting.wav \\\n  -F model=funasr \\\n  -F level=paraformer\n\n# 省略 -F level 即使用引擎默认档`
                  :
                  `# WhisperX：默认档 large-v3，已烘焙 ${whisperxBakedStr}\ncurl http://localhost:8080/v1/audio/transcriptions \\\n  -H "Authorization: Bearer <ASR_API_TOKEN>" \\\n  -F file=@meeting.wav \\\n  -F model=whisperx \\\n  -F level=large-v3\n\n# 省略 -F level 即使用引擎默认档`
                } />
              </Stack>
            </Paper>
          </Stack>
        </Container>
      </Box>
    </ThemeProvider>
  )
}

function ApiEndpoint({ method, path, description, example }) {
  return (
    <Accordion disableGutters elevation={0} sx={{ border: '1px solid', borderColor: 'divider', borderRadius: '8px !important', '&:before': { display: 'none' } }}>
      <AccordionSummary expandIcon={<ExpandMoreIcon />}>
        <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap">
          <Chip label={method} color={method === 'POST' ? 'primary' : 'default'} size="small" sx={{ fontWeight: 700 }} />
          <Typography component="code" fontFamily="monospace" fontWeight={700}>{path}</Typography>
        </Stack>
      </AccordionSummary>
      <AccordionDetails>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>{description}</Typography>
        <Box component="pre" sx={{ m: 0, p: 2, overflowX: 'auto', borderRadius: 2, bgcolor: 'grey.100', fontFamily: 'monospace', fontSize: 13, whiteSpace: 'pre-wrap' }}>
          {example}
        </Box>
      </AccordionDetails>
    </Accordion>
  )
}

function readText(body) {
  try {
    return JSON.parse(body).text
  } catch {
    return ''
  }
}

function readError(body) {
  try {
    return JSON.parse(body).error
  } catch {
    return ''
  }
}

createRoot(document.getElementById('root')).render(<App />)
