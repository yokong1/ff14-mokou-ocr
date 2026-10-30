// ocr_host.cs - 常驻 OCR 服务 (Windows.Media.Ocr + GDI 截屏)
// 协议: 从 stdin 逐行读命令, 从 stdout 逐行回 JSON (UTF-8)
//   OCR  <x> <y> <w> <h> [scale]   截屏指定屏幕区域并识别
//   FILE <path> [scale]            识别图片文件
//   SAVE <path>                    把上一次 OCR 的原始截图存成 png
//   INFO                           返回环境信息
//   QUIT                           退出
using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading.Tasks;
using Windows.Graphics.Imaging;
using Windows.Media.Ocr;
using Windows.Storage.Streams;

static class OcrHost
{
    [DllImport("user32.dll")] static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] static extern int GetSystemMetrics(int nIndex);

    static OcrEngine engine;
    static Bitmap lastBitmap;
    static StreamWriter outw;

    static void Main(string[] args)
    {
        try { SetProcessDPIAware(); } catch { }
        Console.OutputEncoding = new UTF8Encoding(false);
        outw = new StreamWriter(Console.OpenStandardOutput(), new UTF8Encoding(false), 65536);
        outw.AutoFlush = true;
        var inw = new StreamReader(Console.OpenStandardInput(), new UTF8Encoding(false), false, 65536);

        try { engine = OcrEngine.TryCreateFromLanguage(new Windows.Globalization.Language("zh-Hans-CN")); }
        catch (Exception e) { WriteJson(Err("engine init: " + e.Message)); return; }
        if (engine == null) { WriteJson(Err("no zh OCR engine")); return; }

        WriteJson("{\"ok\":true,\"ready\":true,\"maxdim\":" + OcrEngine.MaxImageDimension +
                  ",\"screen\":[" + GetSystemMetrics(0) + "," + GetSystemMetrics(1) + "]}");

        string line;
        while ((line = inw.ReadLine()) != null)
        {
            line = line.Trim();
            if (line.Length == 0) continue;
            string[] p = line.IndexOf('\t') >= 0 ? line.Split('\t') : line.Split(new char[] { ' ' }, StringSplitOptions.RemoveEmptyEntries);
            string cmd = p[0].ToUpperInvariant();
            if (cmd == "QUIT") break;
            try
            {
                if (cmd == "INFO") { WriteJson("{\"ok\":true,\"screen\":[" + GetSystemMetrics(0) + "," + GetSystemMetrics(1) + "]}"); }
                else if (cmd == "OCR" || cmd == "OCRP" || cmd == "OCRC" || cmd == "OCRW")
                {
                    int x = int.Parse(p[1]), y = int.Parse(p[2]), w = int.Parse(p[3]), h = int.Parse(p[4]);
                    double scale = p.Length > 5 ? double.Parse(p[5], CultureInfo.InvariantCulture) : 2.0;
                    var sw = System.Diagnostics.Stopwatch.StartNew();
                    Bitmap bmp = Grab(x, y, w, h);
                    if (lastBitmap != null) lastBitmap.Dispose();
                    lastBitmap = bmp;
                    long capMs = sw.ElapsedMilliseconds;
                    var res = RecognizeProfile(bmp,scale,cmd);
                    sw.Stop();
                    res = res.Insert(1, "\"cap\":" + capMs + ",\"ms\":" + sw.ElapsedMilliseconds + ",");
                    WriteJson(res);
                }
                else if (cmd == "FILE" || cmd == "FILEP" || cmd == "FILEC" || cmd == "FILEW")
                {
                    double scale = p.Length > 2 ? double.Parse(p[2], CultureInfo.InvariantCulture) : 2.0;
                    var sw = System.Diagnostics.Stopwatch.StartNew();
                    Bitmap bmp = new Bitmap(p[1]);
                    if (lastBitmap != null) lastBitmap.Dispose();
                    lastBitmap = bmp;
                    var res = RecognizeProfile(bmp,scale,cmd);
                    sw.Stop();
                    res = res.Insert(1, "\"ms\":" + sw.ElapsedMilliseconds + ",");
                    WriteJson(res);
                }
                else if (cmd == "RETRY")
                {
                    if (lastBitmap == null) { WriteJson(Err("no image")); }
                    else { WriteJson(RecognizePadded(lastBitmap,double.Parse(p[1],CultureInfo.InvariantCulture))); }
                }
                else if (cmd == "CONTRAST")
                {
                    if (lastBitmap == null) { WriteJson(Err("no image")); }
                    else
                    {
                        const int padding = 16;
                        using (var contrast = ContrastText(lastBitmap, padding))
                        {
                            var res = Recognize(contrast, double.Parse(p[1], CultureInfo.InvariantCulture));
                            WriteJson(res.Insert(1, "\"padding\":"+padding+","));
                        }
                    }
                }
                else if (cmd == "COLOR")
                {
                    if (lastBitmap == null) { WriteJson(Err("no image")); }
                    else
                    {
                        const int padding = 16;
                        using (var mask = WhiteTextMask(lastBitmap, padding))
                        {
                            var res = Recognize(mask, 1.0);
                            WriteJson(res.Insert(1, "\"padding\":" + padding + ","));
                        }
                    }
                }
                else if (cmd == "SAVE")
                {
                    if (lastBitmap == null) { WriteJson(Err("no image")); }
                    else { lastBitmap.Save(p[1], ImageFormat.Png); WriteJson("{\"ok\":true}"); }
                }
                else WriteJson(Err("unknown cmd " + cmd));
            }
            catch (Exception e)
            {
                WriteJson(Err(e.GetType().Name + ": " + e.Message));
            }
        }
    }

    static Bitmap Grab(int x, int y, int w, int h)
    {
        if (w <= 0 || h <= 0) throw new ArgumentException("bad region");
        var bmp = new Bitmap(w, h, PixelFormat.Format32bppArgb);
        using (var g = Graphics.FromImage(bmp))
        {
            g.CopyFromScreen(x, y, 0, 0, new Size(w, h), CopyPixelOperation.SourceCopy);
        }
        return bmp;
    }

    static Bitmap WhiteTextMask(Bitmap source, int padding)
    {
        // 浅色字转为黑字白底，消除黄色等底色干扰；原截图仍保留供预览。
        var mask = new Bitmap(source.Width + padding*2, source.Height + padding*2, PixelFormat.Format32bppArgb);
        using (var input = source.Clone(new Rectangle(0, 0, source.Width, source.Height), PixelFormat.Format32bppArgb))
        {
            var src = input.LockBits(new Rectangle(0, 0, input.Width, input.Height), ImageLockMode.ReadOnly, PixelFormat.Format32bppArgb);
            try
            {
                var dst = mask.LockBits(new Rectangle(0, 0, mask.Width, mask.Height), ImageLockMode.WriteOnly, PixelFormat.Format32bppArgb);
                try
                {
                    byte[] pixels = new byte[Math.Abs(src.Stride)*input.Height];
                    byte[] output = new byte[Math.Abs(dst.Stride)*mask.Height];
                    Marshal.Copy(src.Scan0, pixels, 0, pixels.Length);
                    for (int i=0; i<output.Length; i++) output[i]=255;
                    for (int y=0; y<input.Height; y++)
                    {
                        int from = y*Math.Abs(src.Stride), to = (y+padding)*Math.Abs(dst.Stride)+padding*4;
                        for (int x=0; x<input.Width; x++, from+=4, to+=4)
                        {
                            byte value = pixels[from]>200 && pixels[from+1]>200 && pixels[from+2]>200 ? (byte)0 : (byte)255;
                            output[to]=output[to+1]=output[to+2]=value;
                        }
                    }
                    Marshal.Copy(output, 0, dst.Scan0, output.Length);
                }
                finally { mask.UnlockBits(dst); }
            }
            finally { input.UnlockBits(src); }
        }
        return mask;
    }

    static string RecognizeProfile(Bitmap source,double scale,string command)
    {
        if(command=="OCRP" || command=="FILEP")return RecognizePadded(source,scale);
        bool contrast=command=="OCRC" || command=="FILEC";
        bool white=command=="OCRW" || command=="FILEW";
        if(!contrast && !white)return Recognize(source,scale);
        const int padding=16;
        using(var prepared=contrast?ContrastText(source,padding):WhiteTextMask(source,padding))
            return Recognize(prepared,scale).Insert(1,"\"padding\":"+padding+",\"source_img\":["+source.Width+","+source.Height+"],");
    }

    static string RecognizePadded(Bitmap source,double scale)
    {
        const int padding=12;
        using(var padded=new Bitmap(source.Width+padding*2,source.Height+padding*2,PixelFormat.Format32bppArgb))
        {
            using(var g=Graphics.FromImage(padded)){g.Clear(Color.Black);g.DrawImageUnscaled(source,padding,padding);}
            return Recognize(padded,scale).Insert(1,"\"padding\":"+padding+",\"source_img\":["+source.Width+","+source.Height+"],");
        }
    }

    static Bitmap ContrastText(Bitmap source, int padding)
    {
        // 取绿色通道抑制蓝色底和辉光，按当前图像范围归一化，灰字也不会被固定白字阈值抹掉。
        var image = new Bitmap(source.Width+padding*2, source.Height+padding*2, PixelFormat.Format32bppArgb);
        using (var input = source.Clone(new Rectangle(0,0,source.Width,source.Height), PixelFormat.Format32bppArgb))
        {
            var src = input.LockBits(new Rectangle(0,0,input.Width,input.Height), ImageLockMode.ReadOnly, PixelFormat.Format32bppArgb);
            try
            {
                byte[] pixels = new byte[Math.Abs(src.Stride)*input.Height];
                Marshal.Copy(src.Scan0,pixels,0,pixels.Length);
                int[] histogram = new int[256];
                for(int y=0;y<input.Height;y++) for(int x=0;x<input.Width;x++) histogram[pixels[y*Math.Abs(src.Stride)+x*4+1]]++;
                int total=input.Width*input.Height, low=0, high=255, sum=0;
                for(int i=0;i<256;i++){sum+=histogram[i];if(sum>=Math.Max(1,total/100)){low=i;break;}}
                sum=0;
                for(int i=255;i>=0;i--){sum+=histogram[i];if(sum>=Math.Max(1,total/100)){high=i;break;}}
                int span=Math.Max(1,high-low);
                var dst=image.LockBits(new Rectangle(0,0,image.Width,image.Height),ImageLockMode.WriteOnly,PixelFormat.Format32bppArgb);
                try
                {
                    byte[] output=new byte[Math.Abs(dst.Stride)*image.Height];
                    for(int i=0;i<output.Length;i++)output[i]=255;
                    for(int y=0;y<input.Height;y++)for(int x=0;x<input.Width;x++)
                    {
                        int from=y*Math.Abs(src.Stride)+x*4, to=(y+padding)*Math.Abs(dst.Stride)+(x+padding)*4;
                        int value=255-Math.Min(255,Math.Max(0,(pixels[from+1]-low)*255/span));
                        output[to]=output[to+1]=output[to+2]=(byte)value;
                    }
                    Marshal.Copy(output,0,dst.Scan0,output.Length);
                }
                finally{image.UnlockBits(dst);}
            }
            finally{input.UnlockBits(src);}
        }
        return image;
    }

    static SoftwareBitmap NativeSoftwareBitmap(Bitmap bitmap)
    {
        // 原始倍率直接复制像素，省掉 BMP 编码、内存流和再次解码；缩放仍走原解码器。
        using (var copy = bitmap.Clone(new Rectangle(0,0,bitmap.Width,bitmap.Height),PixelFormat.Format32bppArgb))
        {
            var bits=copy.LockBits(new Rectangle(0,0,copy.Width,copy.Height),ImageLockMode.ReadOnly,PixelFormat.Format32bppArgb);
            try
            {
                int rowBytes=copy.Width*4;
                byte[] pixels=new byte[rowBytes*copy.Height];
                for(int y=0;y<copy.Height;y++)
                    Marshal.Copy(IntPtr.Add(bits.Scan0,y*bits.Stride),pixels,y*rowBytes,rowBytes);
                using(var writer=new DataWriter())
                {
                    writer.WriteBytes(pixels);
                    return SoftwareBitmap.CreateCopyFromBuffer(writer.DetachBuffer(),BitmapPixelFormat.Bgra8,
                        copy.Width,copy.Height,BitmapAlphaMode.Ignore);
                }
            }
            finally{copy.UnlockBits(bits);}
        }
    }

    static string Recognize(Bitmap bmp, double scale)
    {
        int nw = Math.Max(1, (int)Math.Round(bmp.Width * scale));
        int nh = Math.Max(1, (int)Math.Round(bmp.Height * scale));
        int cap = (int)OcrEngine.MaxImageDimension;
        if (nw > cap) { double k = (double)cap / nw; nw = cap; nh = Math.Max(1, (int)(nh * k)); }
        if (nh > cap) { double k = (double)cap / nh; nh = cap; nw = Math.Max(1, (int)(nw * k)); }
        double eff = (double)nw / bmp.Width;

        var prepareClock = System.Diagnostics.Stopwatch.StartNew();
        SoftwareBitmap sb;
        if (nw == bmp.Width && nh == bmp.Height)
        {
            sb = NativeSoftwareBitmap(bmp);
        }
        else
        {
        using (var ms = new MemoryStream())
        {
            bmp.Save(ms, ImageFormat.Bmp);
            ms.Position = 0;
            using (var ras = new MemoryStream(ms.ToArray()))
            {
                using (var stream = ToWinRT(ras.ToArray()))
                {
                var decoderTask = BitmapDecoder.CreateAsync(stream);
                var decoder = Await(decoderTask);
                var transform = new BitmapTransform();
                transform.ScaledWidth = (uint)nw;
                transform.ScaledHeight = (uint)nh;
                transform.InterpolationMode = BitmapInterpolationMode.Linear;
                var task = decoder.GetSoftwareBitmapAsync(BitmapPixelFormat.Bgra8, BitmapAlphaMode.Premultiplied,
                                                          transform, ExifOrientationMode.IgnoreExifOrientation,
                                                          ColorManagementMode.DoNotColorManage);
                sb = Await(task);
                }
            }
        }
        }
        prepareClock.Stop();
        var ocrClock = System.Diagnostics.Stopwatch.StartNew();
        Windows.Media.Ocr.OcrResult ocr;
        using (sb) { ocr = Await(engine.RecognizeAsync(sb)); }
        ocrClock.Stop();

        var sbld = new StringBuilder();
        sbld.Append("{\"ok\":true,\"img\":[" + bmp.Width + "," + bmp.Height + "],\"scale\":" +
                    eff.ToString("0.###", CultureInfo.InvariantCulture) + ",\"prep_ms\":" + prepareClock.Elapsed.TotalMilliseconds.ToString("0.###", CultureInfo.InvariantCulture) +
                    ",\"ocr_ms\":" + ocrClock.Elapsed.TotalMilliseconds.ToString("0.###", CultureInfo.InvariantCulture) + ",\"lines\":[");
        bool first = true;
        foreach (var ln in ocr.Lines)
        {
            if (!first) sbld.Append(',');
            first = false;
            var r = ln.Words.Count > 0 ? Union(ln) : new Windows.Foundation.Rect(0, 0, 0, 0);
            sbld.Append("{\"t\":" + Q(ln.Text) + ",\"x\":" + X(r.X, eff) + ",\"y\":" + X(r.Y, eff) +
                        ",\"w\":" + X(r.Width, eff) + ",\"h\":" + X(r.Height, eff) + ",\"words\":[");
            for (int i = 0; i < ln.Words.Count; i++)
            {
                var wd = ln.Words[i];
                if (i > 0) sbld.Append(',');
                sbld.Append("{\"t\":" + Q(wd.Text) + ",\"x\":" + X(wd.BoundingRect.X, eff) + ",\"y\":" +
                            X(wd.BoundingRect.Y, eff) + ",\"w\":" + X(wd.BoundingRect.Width, eff) +
                            ",\"h\":" + X(wd.BoundingRect.Height, eff) + "}");
            }
            sbld.Append("]}");
        }
        sbld.Append("]}");
        return sbld.ToString();
    }

    static Windows.Foundation.Rect Union(Windows.Media.Ocr.OcrLine ln)
    {
        double x0 = double.MaxValue, y0 = double.MaxValue, x1 = double.MinValue, y1 = double.MinValue;
        foreach (var w in ln.Words)
        {
            var r = w.BoundingRect;
            if (r.X < x0) x0 = r.X;
            if (r.Y < y0) y0 = r.Y;
            if (r.X + r.Width > x1) x1 = r.X + r.Width;
            if (r.Y + r.Height > y1) y1 = r.Y + r.Height;
        }
        if (x0 > x1) return new Windows.Foundation.Rect(0, 0, 0, 0);
        return new Windows.Foundation.Rect(x0, y0, x1 - x0, y1 - y0);
    }

    static string X(double v, double eff)
    {
        return Math.Round(v / eff, 1).ToString("0.#", CultureInfo.InvariantCulture);
    }

    static string Q(string s)
    {
        var b = new StringBuilder("\"");
        foreach (char c in s)
        {
            switch (c)
            {
                case '"': b.Append("\\\""); break;
                case '\\': b.Append("\\\\"); break;
                case '\n': b.Append("\\n"); break;
                case '\r': b.Append("\\r"); break;
                case '\t': b.Append("\\t"); break;
                default:
                    if (c < 0x20) b.Append("\\u" + ((int)c).ToString("x4"));
                    else b.Append(c);
                    break;
            }
        }
        b.Append('"');
        return b.ToString();
    }

    static string Err(string m) { return "{\"ok\":false,\"err\":" + Q(m) + "}"; }

    static void WriteJson(string s) { outw.WriteLine(s); }

    static IRandomAccessStream ToWinRT(byte[] data)
    {
        var ras = new InMemoryRandomAccessStream();
        using (var writer = new DataWriter(ras))
        {
            writer.WriteBytes(data);
            Await(writer.StoreAsync());
            writer.DetachStream();
        }
        ras.Seek(0);
        return ras;
    }

    static T Await<T>(Windows.Foundation.IAsyncOperation<T> op)
    {
        while (op.Status == Windows.Foundation.AsyncStatus.Started) System.Threading.Thread.Sleep(2);
        return op.GetResults();
    }
    static void Await(Windows.Foundation.IAsyncAction op)
    {
        while (op.Status == Windows.Foundation.AsyncStatus.Started) System.Threading.Thread.Sleep(2);
        op.GetResults();
    }
}
